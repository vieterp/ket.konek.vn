"""Công nợ của chứng từ nghiệp vụ khác — phân loại dòng, đối trừ, sổ phụ (7C-3).

Quyết định user 2026-09-05: bút toán gõ thẳng vào TK công nợ **vẫn cho phép**,
nhưng từ nay `general_ledger.journal` là nguồn ghi `ar_ap_ledger` thứ ba, bên
cạnh `purchase` và `sales`. Khoản sinh từ đây là khoản nợ **không có hóa đơn
gốc**: `document_id` trỏ chính chứng từ GLE.

Cả module xoay quanh một phép phân loại duy nhất, `classify`: dòng nào trên
chứng từ chạm công nợ, theo chiều nào, và làm TĂNG hay GIẢM khoản nợ.

* **Dòng chạm công nợ** = TK có `detail_tracking` khai đúng loại đối tác của
  dòng (`customer` với khách hàng, `vendor` với nhà cung cấp). Đọc theo cấu
  hình chứ không theo số hiệu `131`/`331`: số hiệu thuộc gói, và gói tự dựng
  của khách đặt công nợ ở số khác là chuyện bình thường (SRS 19 §9 #1). Nhân
  viên đứng ngoài — tạm ứng chưa có đường đối trừ theo từng lần trong v1.
* **Bên thuận** (Nợ với phải thu, Có với phải trả) làm TĂNG nợ ⇒ sinh một
  khoản mới trên sổ phụ.
* **Bên ngược** làm GIẢM nợ ⇒ phải trỏ vào khoản đang treo, và đi đúng cơ chế
  mà phiếu thu/chi dùng. Bù trừ 131 ↔ 331 của cùng một đối tác vì thế là **hai
  dòng bên ngược**, tức hai lượt đối trừ — không phải hai khoản nợ mới. Không
  phân biệt được hai ca này thì mỗi lượt bù trừ phình cả hai vế sổ phụ.

**Bên ngược mà KHÔNG trỏ đích thì không sinh gì** — đó là khoản khách ứng
trước / trả trước người bán, và nó là **lỗ đã biết**, chung với phiếu thu ứng
trước của `cash_book` hôm nay (`settlements` ở đó cũng tùy chọn). Chặn ở đây
mà thả ở phiếu thu là hai luật cho cùng một hình dạng. Lỗ ấy là điều kiện thứ
năm ghi ở đầu `posting/integrity/checks/arap_matches_control.sql`, đóng ở lát
7C-4 cùng lúc cho mọi phân hệ.

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
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.money import convert_currency
from ket.kernel.protocols import PROVIDERS, SettlementTargetKind, SubledgerEntry
from ket.modules.general_ledger.journal.models import JournalLine, JournalSettlement
from ket.modules.general_ledger.journal.schemas import JournalSettlementIn
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

SETTLEMENT_ON_INCREASE_CODE = "journal.settlement_on_increasing_line"
"""Dòng làm TĂNG nợ mà lại trỏ đích đối trừ — vừa ghi nợ vừa tất toán."""

SETTLEMENT_ON_NON_DEBT_CODE = "journal.settlement_on_non_debt_line"
"""Dòng không chạm TK công nợ mà lại trỏ đích đối trừ."""

SETTLEMENT_OVER_REMAINING_CODE = "journal.settlement_exceeds_remaining"
"""Tổng đối trừ của nhiều DÒNG vào cùng một đích vượt số còn nợ."""

_DIRECTION_BY_PARTNER_KIND: dict[PartnerKind, tuple[str, SettlementTargetKind, bool]] = {
    # đối tác → (token `detail_tracking` phải có, loại đích, bên thuận là bên Nợ)
    PartnerKind.CUSTOMER: (DetailTracking.CUSTOMER, SettlementTargetKind.JOURNAL_RECEIVABLE, True),
    PartnerKind.VENDOR: (DetailTracking.VENDOR, SettlementTargetKind.JOURNAL_PAYABLE, False),
}
"""Nhân viên cố ý vắng mặt: `PartnerKind.EMPLOYEE` không có mặt ở đây nên dòng
tạm ứng 141/334 đi qua `classify` mà không sinh gì — cùng phạm vi với check
toàn vẹn 131/331, vốn cũng để nhân viên ngoài (xem `arap_matches_control.sql`)."""


@dataclass(frozen=True)
class DebtLine:
    """Một dòng định khoản đã được nhận ra là chạm công nợ."""

    line_no: int
    line_id: UUID
    account_id: int
    partner_kind: PartnerKind
    partner_id: int
    target_kind: SettlementTargetKind
    currency_code: str
    exchange_rate: Decimal
    amount_fc: Decimal
    """Số tiền trên bên của chính dòng — luôn dương."""

    increases_debt: bool
    """Bên thuận tính chất công nợ: dòng làm khoản nợ lớn lên."""


def classify(session: Session, lines: Sequence[JournalLine]) -> list[DebtLine]:
    """Lọc ra các dòng chạm công nợ của chứng từ, kèm chiều và bên.

    Đọc dòng **đã lưu** chứ không payload: `sync_after_post` chạy lúc ghi sổ
    và không có payload nào trong tay, nên hai đường (lúc cất, lúc ghi sổ) chỉ
    khớp nhau chắc chắn khi cùng đọc một nguồn. Dòng đã lưu cũng đã giải xong
    `currency_code`/`exchange_rate` rơi từ chứng từ, nên phép phân loại không
    phải lặp lại luật rơi ấy lần thứ hai.

    Một truy vấn cho cả chứng từ (`accounts_by_id`), cùng lối `posting_mapper`.
    """
    accounts = accounts_by_id(session, [line.account_id for line in lines])
    debt_lines: list[DebtLine] = []
    for line in lines:
        if line.partner_id is None or line.partner_kind is None:
            continue
        rule = _DIRECTION_BY_PARTNER_KIND.get(PartnerKind(line.partner_kind))
        if rule is None:
            continue
        tracking_token, target_kind, natural_is_debit = rule
        account = accounts.get(line.account_id)
        if account is None or tracking_token not in (account.detail_tracking or ()):
            continue
        on_debit = line.debit_fc > _ZERO
        amount_fc = line.debit_fc if on_debit else line.credit_fc
        if amount_fc <= _ZERO:
            continue
        debt_lines.append(
            DebtLine(
                line_no=line.line_no,
                line_id=line.id,
                account_id=line.account_id,
                partner_kind=PartnerKind(line.partner_kind),
                partner_id=line.partner_id,
                target_kind=target_kind,
                currency_code=line.currency_code,
                exchange_rate=line.exchange_rate,
                amount_fc=amount_fc,
                increases_debt=(on_debit == natural_is_debit),
            )
        )
    return debt_lines


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
        if debt.increases_debt:
            violations.append(
                PostingViolation(
                    SETTLEMENT_ON_INCREASE_CODE,
                    "Dòng này đang ghi tăng công nợ nên không đối trừ khoản nào — "
                    "đưa số đối trừ sang dòng ghi giảm",
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
    """Khoản nợ MỚI mà chứng từ sinh ra — một dòng cho mỗi dòng ghi tăng nợ.

    Không gộp theo (đối tác, TK) như hóa đơn mua: mỗi dòng của chứng từ GLE là
    một khoản nợ người dùng cố ý tách ra, và gộp lại thì màn đối trừ mất đúng
    cái ranh giới ấy — hai khoản phải thu ghi trong một bút toán phân loại lại
    sẽ chỉ còn một dòng chọn được.
    """
    return [
        SubledgerEntry(
            target_kind=line.target_kind,
            partner_kind=line.partner_kind,
            partner_id=line.partner_id,
            ledger=_FINANCIAL_LEDGER,
            account_id=line.account_id,
            document_no=voucher.voucher_no,
            document_date=voucher.document_date,
            due_date=_due_date_of(session, line, voucher=voucher),
            currency_code=line.currency_code,
            exchange_rate=line.exchange_rate,
            amount_fc=line.amount_fc,
            amount=convert_currency(line.amount_fc, line.exchange_rate, scale),
            description=voucher.description,
        )
        for line in classify(session, lines)
        if line.increases_debt
    ]


_FINANCIAL_LEDGER = 0
"""Chỉ sổ tài chính, cùng luật với `purchase`/`sales`: nguồn đối trừ của
`receivables` chỉ cộng số đã trả vào sổ ấy, nên dòng sổ quản trị sẽ không bao
giờ đóng — báo cáo tuổi nợ sổ quản trị vì thế trống thay vì sai."""


def _due_date_of(session: Session, line: DebtLine, *, voucher: Voucher) -> date | None:
    """Hạn thanh toán rơi về điều khoản của danh mục đối tác (quyết định Q1-B).

    Chứng từ GLE không có ô điều khoản thanh toán, và một khoản nợ không có
    hạn thì **không bao giờ kêu quá hạn** — nó nằm mãi ở cột "chưa đến hạn"
    của báo cáo tuổi nợ và ngoài tầm guard ngưỡng nợ. Rơi về danh mục là cùng
    luật FR-SAL-009 mà hóa đơn bán đã dùng từ 7C-2, đổi lấy nguồn: ở đó là
    khách hàng của hóa đơn, ở đây là đối tác của chính dòng.

    Đối tác chưa khai điều khoản → để trống, không đoán.
    """
    term_id = session.execute(
        select(Partner.payment_term_id).where(Partner.id == line.partner_id)
    ).scalar_one_or_none()
    if term_id is None:
        return None
    term = session.get(PaymentTerm, term_id)
    if term is None:
        return None
    return voucher.document_date + timedelta(days=term.due_days)


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
