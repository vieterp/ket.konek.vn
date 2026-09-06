"""Công nợ của chứng từ nghiệp vụ khác — phân loại dòng, đối trừ, sổ phụ (7C-3).

Quyết định user 2026-09-05: bút toán gõ thẳng vào TK công nợ **vẫn cho phép**,
nhưng từ nay `general_ledger.journal` là nguồn ghi `ar_ap_ledger` thứ ba, bên
cạnh `purchase` và `sales`. Khoản sinh từ đây là khoản nợ **không có hóa đơn
gốc**: `document_id` trỏ chính chứng từ GLE.

Phép phân loại dòng (dòng nào chạm công nợ, theo chiều nào, làm TĂNG hay GIẢM
khoản nợ) **đã dời lên `ket.posting.debt_lines` ở lát 7C-4**, khi phiếu thu/chi
và chứng từ ngân hàng ghi cùng sổ phụ theo cùng luật ấy — xem docstring tệp kia
cho luật đầy đủ. Ở đây còn lại phần buộc vào bảng `gl_journal_settlements` và
hình dạng payload chứng từ.

Bù trừ 131 ↔ 331 của cùng một đối tác là **hai dòng bên ngược**, tức hai lượt
đối trừ — không phải hai khoản nợ mới. Bù khoản khách ứng trước với hóa đơn
phát sinh sau (7C-4, quyết định user 2026-09-06) là **một chứng từ của chính
module này**: dòng bên thuận trỏ vào khoản ứng trước, dòng bên ngược trỏ vào
hóa đơn — hai lượt đối trừ trên một chứng từ, sổ cái không đổi vì cả hai bút
toán đã ghi từ trước.

**BR-QUY-03 áp cho TỪNG DÒNG.** Ba phân hệ trước so tổng đối trừ với tổng tiền
*chứng từ* vì mỗi chứng từ ấy chỉ có một đối tác và một TK công nợ. Chứng từ
GLE chạm nhiều đối tác cùng lúc, và `posting.settlements.price_settlements`
nhận đúng một `partner_id` cho cả lượt gọi — nên ở đây gọi **mỗi dòng một
lượt**, với "tổng tiền chứng từ" là số tiền của chính dòng ấy. Vi phạm của
từng lượt được gom lại rồi ném **một lần**, giữ nguyên triết lý "trả trọn bộ
vi phạm" của bộ kiểm phase-04.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.money import convert_currency
from ket.kernel.protocols import PROVIDERS, SettlementTargetKind, SubledgerEntry
from ket.modules.general_ledger.journal.models import JournalLine, JournalSettlement
from ket.modules.general_ledger.journal.schemas import JournalSettlementIn
from ket.posting.debt_lines import DebtLine, DebtSide
from ket.posting.debt_lines import classify as classify_sides
from ket.posting.debt_lines import subledger_entries as build_subledger_entries
from ket.posting.documents.models import Voucher
from ket.posting.settlements import (
    PricedSettlement,
    apply_settlement_rows,
    revert_settlement_rows,
)
from ket.posting.settlements import (
    price_settlements as price_settlement_inputs,
)

_ZERO = Decimal(0)

SETTLEMENT_ON_NON_DEBT_CODE = "journal.settlement_on_non_debt_line"
"""Dòng không chạm TK công nợ mà lại trỏ đích đối trừ."""

SETTLEMENT_OVER_REMAINING_CODE = "journal.settlement_exceeds_remaining"
"""Tổng đối trừ của nhiều DÒNG vào cùng một đích vượt số còn nợ."""


def _sides_of(lines: Sequence[JournalLine]) -> list[DebtSide]:
    """Dòng định khoản **một bên** → hình dạng chung của `posting.debt_lines`.

    Dòng GLE đã mang sẵn một bên (`debit_fc` hoặc `credit_fc`), tiền tệ và tỷ
    giá đã rơi từ chứng từ lúc lưu — nên phép quy đổi ở đây chỉ là đổi tên
    trường, không có luật nào.
    """
    sides: list[DebtSide] = []
    for line in lines:
        if line.partner_id is None or line.partner_kind is None:
            continue
        on_debit = line.debit_fc > _ZERO
        amount_fc = line.debit_fc if on_debit else line.credit_fc
        sides.append(
            DebtSide(
                line_no=line.line_no,
                account_id=line.account_id,
                partner_kind=PartnerKind(line.partner_kind),
                partner_id=line.partner_id,
                on_debit=on_debit,
                amount_fc=amount_fc,
                currency_code=line.currency_code,
                exchange_rate=line.exchange_rate,
            )
        )
    return sides


def classify(session: Session, lines: Sequence[JournalLine]) -> list[DebtLine]:
    """Lọc ra các dòng chạm công nợ của chứng từ, kèm chiều và bên.

    Đọc dòng **đã lưu** chứ không payload: `sync_after_post` chạy lúc ghi sổ
    và không có payload nào trong tay, nên hai đường (lúc cất, lúc ghi sổ) chỉ
    khớp nhau chắc chắn khi cùng đọc một nguồn.
    """
    return classify_sides(session, _sides_of(lines))


def price_settlements(
    session: Session,
    *,
    lines: Sequence[JournalLine],
    settlements: Sequence[JournalSettlementIn],
    branch_id: int,
    scale: int,
) -> dict[int, list[PricedSettlement]]:
    """Kiểm + định giá dòng đối trừ, trả kết quả theo từng `line_no`.

    Ném **một** `PostingValidationError` gom mọi vi phạm của mọi dòng.
    """
    by_line: dict[int, list[JournalSettlementIn]] = {}
    for row in settlements:
        by_line.setdefault(row.line_no, []).append(row)
    if not by_line:
        return {}

    debt_by_line = {line.line_no: line for line in classify(session, lines)}
    violations: list[PostingViolation] = []
    priced_by_line: dict[int, list[PricedSettlement]] = {}

    for line_no, rows in sorted(by_line.items()):
        debt = debt_by_line.get(line_no)
        if debt is None:
            violations.append(
                PostingViolation(
                    SETTLEMENT_ON_NON_DEBT_CODE,
                    "Chỉ dòng hạch toán vào tài khoản công nợ có theo dõi đối tác mới đối trừ được",
                    line_no=line_no,
                )
            )
            continue
        try:
            priced = price_settlement_inputs(
                session,
                settlements=rows,
                # "Tổng tiền chứng từ" của BR-QUY-03 thu hẹp về tổng tiền của
                # chính dòng: đối trừ ở đây thuộc về dòng, không thuộc chứng từ.
                lines_total_fc=debt.amount_fc,
                partner_id=debt.partner_id,
                partner_kind=debt.partner_kind,
                branch_id=branch_id,
                currency_code=debt.currency_code,
                exchange_rate=debt.exchange_rate,
                scale=scale,
                # Khoản đích phải treo đúng TK mà dòng này ghi giảm — khác TK
                # là sổ cái giảm một TK còn sổ phụ giảm TK kia.
                account_id=debt.account_id,
                # Bên THUẬN tất toán khoản ứng trước, bên NGƯỢC tất toán khoản
                # nợ. Bản 7C-3 từ chối thẳng bên thuận vì lúc ấy khoản ứng
                # trước chưa có dòng sổ phụ nào để trỏ tới; từ 7C-4 nó có, và
                # chính chứng từ này là đường bù khoản ứng trước với hóa đơn.
                settles_advance=debt.settles_advance,
            )
        except PostingValidationError as error:
            violations.extend(
                # Gắn số dòng vào từng vi phạm: người dùng nhìn thấy lưới, và
                # "đích không tồn tại" mà không nói ở dòng nào thì vô dụng trên
                # một chứng từ chạm nhiều đối tác.
                _with_line_no(violation, line_no)
                for violation in error.violations
            )
            continue
        priced_by_line[line_no] = _absorb_rounding(priced, debt, scale=scale)

    violations.extend(_cross_line_overpay(session, payload_settlements=settlements))

    if violations:
        raise PostingValidationError(
            "Dòng đối trừ công nợ trên chứng từ chưa hợp lệ", violations=violations
        )
    return priced_by_line


def _cross_line_overpay(
    session: Session, *, payload_settlements: Sequence[JournalSettlementIn]
) -> list[PostingViolation]:
    """Tổng đối trừ của CẢ chứng từ vào một đích không được vượt số còn nợ.

    `posting.settlements.price_settlements` kiểm từng dòng một, và ở ba phân hệ
    trước thế là đủ vì mỗi chứng từ chỉ có một khối đối trừ. Chứng từ GLE thì
    có nhiều dòng, và hai dòng cùng trỏ vào một hóa đơn 100 với 60 mỗi dòng đều
    lọt phép kiểm từng-dòng — lượt CẤT thành công, rồi lượt GHI SỔ mới nổ 422 ở
    `apply` (hoặc `CHECK settled <= amount` của DB). Lỗi phải nói ngay lúc cất,
    ở đúng chỗ người dùng còn nhìn thấy lưới.
    """
    totals: dict[tuple[SettlementTargetKind, UUID], Decimal] = {}
    for row in payload_settlements:
        key = (row.target_kind, row.target_id)
        totals[key] = totals.get(key, _ZERO) + row.amount_fc

    by_kind: dict[SettlementTargetKind, list[UUID]] = {}
    for kind, target_id in totals:
        by_kind.setdefault(kind, []).append(target_id)

    violations: list[PostingViolation] = []
    for kind, target_ids in by_kind.items():
        source = PROVIDERS.settlement_source(kind)
        if source is None:
            # Loại không có chủ đã được vòng kiểm từng dòng báo — không nói lại.
            continue
        for invoice in source.find(session, target_ids=target_ids):
            total = totals.get((invoice.target_kind, invoice.target_id))
            if total is not None and total > invoice.remaining_fc:
                violations.append(
                    PostingViolation(
                        SETTLEMENT_OVER_REMAINING_CODE,
                        "Tổng số đối trừ của chứng từ vào chứng từ công nợ này vượt số còn nợ",
                        target_kind=invoice.target_kind.value,
                        target_id=str(invoice.target_id),
                        settled_fc=str(total),
                        remaining_fc=str(invoice.remaining_fc),
                    )
                )
    return violations


def _with_line_no(violation: PostingViolation, line_no: int) -> PostingViolation:
    """Dựng lại vi phạm kèm số dòng — `PostingViolation` là lớp thường, không
    dataclass, nên không `replace` được."""
    if violation.line_no is not None:
        return violation
    return PostingViolation(
        violation.code,
        violation.message,
        ledger=violation.ledger,
        line_no=line_no,
        **violation.details,
    )


def _absorb_rounding(
    priced: list[PricedSettlement], debt: DebtLine, *, scale: int
) -> list[PricedSettlement]:
    """Dồn phần lẻ làm tròn vào dòng đối trừ cuối của cùng một dòng định khoản.

    Sổ cái quy đổi **một lần** cho cả dòng (`convert_currency` trong
    `posting_mapper`), còn đối trừ quy đổi **từng hóa đơn đích** — hai cách
    chẻ khác nhau lệch vài đồng trên chứng từ ngoại tệ, và phần lệch ấy là số
    treo mãi trên hóa đơn gốc mà không ai đối trừ được. Cùng doctrine với
    `purchase.settlement_service.price_settlements`; phần lẻ vào cả `amount`
    lẫn `fx_diff` nên `settled` (= `amount − fx_diff`) không đổi.
    """
    if not priced:
        return priced
    remainder = convert_currency(debt.amount_fc, debt.exchange_rate, scale) - sum(
        (row.amount for row in priced), _ZERO
    )
    if remainder:
        last = priced[-1]
        priced[-1] = replace(last, amount=last.amount + remainder, fx_diff=last.fx_diff + remainder)
    return priced


def subledger_entries(
    session: Session,
    voucher: Voucher,
    lines: Sequence[JournalLine],
    *,
    scale: int,
) -> list[SubledgerEntry]:
    """Khoản MỚI mà chứng từ sinh ra — một dòng cho mỗi bên KHÔNG trỏ đích.

    Bên thuận không đích ⇒ khoản nợ mới; bên ngược không đích ⇒ khoản ứng
    trước (lát 7C-4 — trước đó ca ấy không sinh gì, và đó chính là lỗ #2 của
    `arap_matches_control`). Bên có trỏ đích là một lượt ĐỐI TRỪ, không sinh
    khoản nào, nên bị loại ở đây.

    Đọc dòng đối trừ **đã lưu** chứ không payload, cùng lý do với `classify`:
    hàm này chạy ở `sync_after_post`, nơi không có payload nào trong tay.
    """
    settled_line_ids = {row.journal_line_id for row in _stored(session, voucher.id)}
    settled_line_nos = {line.line_no for line in lines if line.id in settled_line_ids}
    return build_subledger_entries(
        session,
        voucher=voucher,
        debt_lines=[
            line for line in classify(session, lines) if line.line_no not in settled_line_nos
        ],
        scale=scale,
    )


def write_settlements(
    session: Session,
    *,
    voucher_id: UUID,
    priced_by_line: dict[int, list[PricedSettlement]],
    line_ids_by_no: dict[int, UUID],
) -> None:
    """Thay trọn bộ dòng đối trừ đã lưu của chứng từ.

    Thay trọn chứ không diff: `service.update` đã thay trọn bộ dòng định khoản
    (id cũ biến mất), nên mọi dòng đối trừ trỏ vào id cũ cũng phải đi cùng.
    """
    for stored in _stored(session, voucher_id):
        session.delete(stored)
    session.flush()
    for line_no, priced in sorted(priced_by_line.items()):
        for row in priced:
            session.add(
                JournalSettlement(
                    voucher_id=voucher_id,
                    journal_line_id=line_ids_by_no[line_no],
                    target_kind=row.target_kind.value,
                    target_id=row.target_id,
                    amount_fc=row.amount_fc,
                    amount=row.amount,
                    fx_diff=row.fx_diff,
                )
            )
    session.flush()


def _stored(session: Session, voucher_id: UUID) -> Sequence[JournalSettlement]:
    return (
        session.execute(select(JournalSettlement).where(JournalSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )


def stored_settlements(session: Session, voucher_id: UUID) -> Sequence[JournalSettlement]:
    """Dòng đối trừ đã lưu — cho tầng API đọc lại."""
    return _stored(session, voucher_id)


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã trả vào khoản đích — chạy SAU `PostingService.post`."""
    apply_settlement_rows(session, _stored(session, voucher_id))


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ đúng số đã cộng — chạy SAU `PostingService.unpost`."""
    revert_settlement_rows(session, _stored(session, voucher_id))


def line_ids_by_no(session: Session, voucher_id: UUID) -> dict[int, UUID]:
    """`line_no` → id dòng đã lưu, để nối dòng đối trừ vào dòng định khoản."""
    rows = session.execute(
        select(JournalLine.line_no, JournalLine.id).where(JournalLine.voucher_id == voucher_id)
    ).all()
    return {row.line_no: row.id for row in rows}
