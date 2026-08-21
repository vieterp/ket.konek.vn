"""Đối trừ công nợ dùng chung cho chứng từ tiền (`docs/srs/03` §4, FR-BNK-007).

Tách từ `modules/cash_book/settlement_service.py` ở lát 6C: phiếu thu/chi tiền
mặt và chứng từ tiền gửi (báo có, ủy nhiệm chi, séc) dùng CÙNG một cơ chế đối
trừ — mà `bank` không được import `cash_book` (luật phụ thuộc #1), nên phần
không-phụ-thuộc-module sống ở `ket.posting`, tầng cả hai cùng import được.
Mỗi module giữ bảng đối trừ riêng (`cash_settlements`/`bank_settlements`, FK
trỏ đúng bảng thân của mình) và một lớp vỏ mỏng gọi vào đây.

Ba việc, ba thời điểm (giữ nguyên từ 6B):

* **Lúc cất** — `price_settlements`: tra đích qua `SettlementTargetSource`
  (kernel Protocol), kiểm khớp đối tác/chi nhánh/tiền tệ/số còn nợ (BR-QUY-02)
  + tổng đối trừ bằng tổng chứng từ (BR-QUY-03), rồi **định giá**: `amount` =
  VND theo tỷ giá chứng từ, `fx_diff` = chênh với VND theo tỷ giá ghi nhận.
  Làm tròn xảy ra đúng một lần ở đây.
* **Lúc ghi sổ** — `apply_settlement_rows`: cộng số đã trả vào từng đích
  (source tự khóa `FOR UPDATE` + kiểm vượt lần cuối).
* **Lúc bỏ ghi sổ** — `revert_settlement_rows`: gỡ đúng số đã cộng.

Kèm `fx_adjustment_lines` — cặp dòng chênh lệch tỷ giá thu/trả tiền
(FR-SYS-066) cho posting mapper của cả hai module: bốn hướng dấu quy về một
luật LÃI → TK công nợ bên Nợ / 515 bên Có, LỖ → 635 bên Nợ / TK công nợ bên
Có; hướng lãi/lỗ phụ thuộc **chiều tiền** (`money_in`), không chỉ dấu chênh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.accounts_provider import default_account, resolve_package
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.money import convert_currency
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.protocols import (
    PROVIDERS,
    OpenInvoice,
    SettlementTargetKind,
    SettlementTargetSource,
)
from ket.posting.contracts import PostingDimensions, PostingLine
from ket.posting.documents.models import Voucher

SETTLEMENT_NO_SOURCE_CODE = "settlement.kind_unavailable"
SETTLEMENT_TARGET_MISSING_CODE = "settlement.target_missing"
SETTLEMENT_PARTNER_MISMATCH_CODE = "settlement.partner_mismatch"
SETTLEMENT_BRANCH_MISMATCH_CODE = "settlement.branch_mismatch"
SETTLEMENT_CURRENCY_MISMATCH_CODE = "settlement.currency_mismatch"
SETTLEMENT_OVER_REMAINING_CODE = "settlement.exceeds_remaining"
SETTLEMENT_PARTNER_REQUIRED_CODE = "settlement.partner_required"
SETTLEMENT_TOTAL_MISMATCH_CODE = "settlement.total_mismatch"

FX_GAIN_PURPOSE = "fx_gain"
FX_LOSS_PURPOSE = "fx_loss"
FX_ACCOUNT_MISSING_CODE = "cash.fx_account_missing"
"""Mã giữ nguyên chuỗi từ 6B — nó đã nằm trong hợp đồng lỗi của API phiếu quỹ."""

_EMPTY_DIMENSIONS = PostingDimensions()
_ONE = Decimal(1)


class SettlementInput(Protocol):
    """Một dòng đối trừ người dùng nhập — hình dạng tối thiểu mà pricing cần.

    Protocol chứ không BaseModel: mỗi module đã có Pydantic schema riêng ở ranh
    giới API (`CashSettlementIn`, `BankSettlementIn`) và chúng thỏa sẵn.
    """

    @property
    def target_kind(self) -> SettlementTargetKind: ...
    @property
    def target_id(self) -> UUID: ...
    @property
    def amount_fc(self) -> Decimal: ...


class SettlementRow(Protocol):
    """Một dòng đối trừ đã lưu — hình dạng chung của bản ghi ORM hai module."""

    @property
    def target_kind(self) -> int: ...
    @property
    def target_id(self) -> UUID: ...
    @property
    def amount_fc(self) -> Decimal: ...
    @property
    def amount(self) -> Decimal: ...
    @property
    def fx_diff(self) -> Decimal: ...


@dataclass(frozen=True)
class PricedSettlement:
    """Một dòng đối trừ đã định giá xong — thứ service module ghi vào bảng mình."""

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


def price_settlements(
    session: Session,
    *,
    settlements: Sequence[SettlementInput],
    lines_total_fc: Decimal,
    partner_id: int | None,
    partner_kind: PartnerKind | None,
    branch_id: int,
    currency_code: str,
    exchange_rate: Decimal,
    scale: int,
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một chứng từ. Trả toàn bộ vi
    phạm một lượt (triết lý bộ kiểm phase-04), không nhỏ giọt."""
    if not settlements:
        return []

    violations: list[PostingViolation] = []
    if partner_id is None or partner_kind is None:
        violations.append(
            PostingViolation(
                SETTLEMENT_PARTNER_REQUIRED_CODE,
                "Chứng từ có đối trừ công nợ phải chọn đối tác",
            )
        )

    invoices = _find_input_targets(session, settlements, violations)

    total_settled = Decimal(0)
    priced: list[PricedSettlement] = []
    for row in settlements:
        invoice = invoices.get((row.target_kind, row.target_id))
        if invoice is None:
            continue
        _check_target(
            invoice,
            amount_fc=row.amount_fc,
            partner_id=partner_id,
            partner_kind=partner_kind,
            branch_id=branch_id,
            currency_code=currency_code,
            violations=violations,
        )
        amount = convert_currency(row.amount_fc, exchange_rate, scale)
        # VND giải phóng trên sổ: theo tỷ giá ghi nhận, nhưng KHÔNG được vượt
        # số VND còn treo, và lát đối trừ CUỐI (vét sạch nguyên tệ) phải gánh
        # trọn phần lẻ còn lại — nếu không, tổng các phần làm-tròn-riêng lệch
        # vài đồng so với giá trị ghi nhận và hoặc CHECK DB nổ (thu), hoặc sổ
        # treo mãi vài đồng không ai đối trừ được (review 6B, H-2). Phần lẻ
        # dồn vào `fx_diff` — đi vào 515/635 cùng chỗ với chênh lệch tỷ giá,
        # đúng bản chất "chênh lệch do làm tròn khi thanh toán".
        recognition = convert_currency(row.amount_fc, invoice.exchange_rate, scale)
        if row.amount_fc == invoice.remaining_fc:
            recognition = invoice.remaining
        else:
            recognition = min(recognition, invoice.remaining)
        priced.append(
            PricedSettlement(
                target_kind=row.target_kind,
                target_id=row.target_id,
                amount_fc=row.amount_fc,
                amount=amount,
                fx_diff=amount - recognition,
            )
        )
        total_settled += row.amount_fc

    # BR-QUY-03: tổng cột "Số thu" phải bằng tổng tiền trên chứng từ — chỉ so
    # khi mọi đích đều tra được, nếu không thông điệp "lệch tổng" chỉ là tiếng
    # vọng của lỗi thiếu đích ở trên.
    if not violations and total_settled != lines_total_fc:
        violations.append(
            PostingViolation(
                SETTLEMENT_TOTAL_MISMATCH_CODE,
                "Tổng số đối trừ phải bằng tổng tiền trên chứng từ",
                settled_fc=str(total_settled),
                voucher_fc=str(lines_total_fc),
            )
        )

    if violations:
        raise PostingValidationError(
            "Dòng đối trừ công nợ trên chứng từ chưa hợp lệ", violations=violations
        )
    return priced


def _find_input_targets(
    session: Session,
    settlements: Sequence[SettlementInput],
    violations: list[PostingViolation],
) -> dict[tuple[SettlementTargetKind, UUID], OpenInvoice]:
    """Tra mọi đích theo loại; loại chưa có chủ hoặc đích biến mất → vi phạm."""
    by_kind: dict[SettlementTargetKind, list[UUID]] = {}
    for row in settlements:
        by_kind.setdefault(row.target_kind, []).append(row.target_id)

    found: dict[tuple[SettlementTargetKind, UUID], OpenInvoice] = {}
    for kind, target_ids in by_kind.items():
        source = PROVIDERS.settlement_source(kind)
        if source is None:
            violations.append(
                PostingViolation(
                    SETTLEMENT_NO_SOURCE_CODE,
                    "Loại chứng từ công nợ này chưa có phân hệ quản lý trong bản cài",
                    target_kind=kind.value,
                )
            )
            continue
        invoices = {
            invoice.target_id: invoice for invoice in source.find(session, target_ids=target_ids)
        }
        for target_id in target_ids:
            invoice = invoices.get(target_id)
            if invoice is None:
                violations.append(
                    PostingViolation(
                        SETTLEMENT_TARGET_MISSING_CODE,
                        "Chứng từ công nợ được đối trừ không còn tồn tại — chọn lại trên phiếu",
                        target_kind=kind.value,
                        target_id=str(target_id),
                    )
                )
            else:
                found[(kind, target_id)] = invoice
    return found


def _check_target(
    invoice: OpenInvoice,
    *,
    amount_fc: Decimal,
    partner_id: int | None,
    partner_kind: PartnerKind | None,
    branch_id: int,
    currency_code: str,
    violations: list[PostingViolation],
) -> None:
    if (
        partner_kind is not None
        and partner_id is not None
        and (invoice.partner_kind, invoice.partner_id) != (partner_kind, partner_id)
    ):
        violations.append(
            PostingViolation(
                SETTLEMENT_PARTNER_MISMATCH_CODE,
                "Chứng từ công nợ thuộc đối tác khác với đối tác trên chứng từ",
                target_id=str(invoice.target_id),
            )
        )
    if invoice.branch_id != branch_id:
        violations.append(
            PostingViolation(
                SETTLEMENT_BRANCH_MISMATCH_CODE,
                "Chứng từ công nợ thuộc chi nhánh khác với chi nhánh của chứng từ",
                target_id=str(invoice.target_id),
            )
        )
    if invoice.currency_code != currency_code:
        # Đối trừ chéo tiền tệ (thu VND cho nợ USD) cần tỷ giá thỏa thuận riêng
        # từng dòng — ngoài phạm vi v1, ghi ở báo cáo lát 6B.
        violations.append(
            PostingViolation(
                SETTLEMENT_CURRENCY_MISMATCH_CODE,
                "Chứng từ công nợ ghi bằng loại tiền khác với chứng từ",
                target_id=str(invoice.target_id),
                invoice_currency=invoice.currency_code,
                voucher_currency=currency_code,
            )
        )
    if amount_fc > invoice.remaining_fc:
        violations.append(
            PostingViolation(
                SETTLEMENT_OVER_REMAINING_CODE,
                "Số đối trừ vượt số còn nợ của chứng từ công nợ (BR-QUY-02)",
                target_id=str(invoice.target_id),
                remaining_fc=str(invoice.remaining_fc),
                amount_fc=str(amount_fc),
            )
        )


def _require_source(kind: int) -> SettlementTargetSource:
    source = PROVIDERS.settlement_source(SettlementTargetKind(kind))
    if source is None:
        # Dòng đối trừ đã được cất với source còn sống; tới lúc ghi sổ mà source
        # biến mất là cấu hình tiến trình hỏng, không phải lỗi người dùng.
        raise RuntimeError(f"Không có SettlementTargetSource cho target_kind={kind}")
    return source


def apply_settlement_rows(session: Session, rows: Sequence[SettlementRow]) -> None:
    """Cộng số đã trả vào từng đích — chạy SAU `PostingService.post`, cùng transaction."""
    for row in rows:
        _require_source(row.target_kind).apply(
            session,
            target_id=row.target_id,
            amount_fc=row.amount_fc,
            amount=row.amount - row.fx_diff,
        )


def revert_settlement_rows(session: Session, rows: Sequence[SettlementRow]) -> None:
    """Gỡ số đã trả — chạy SAU `PostingService.unpost`, cùng transaction."""
    for row in rows:
        _require_source(row.target_kind).revert(
            session,
            target_id=row.target_id,
            amount_fc=row.amount_fc,
            amount=row.amount - row.fx_diff,
        )


def open_invoices(
    session: Session,
    *,
    side: str,
    partner_kind: PartnerKind,
    partner_id: int,
    branch_id: int,
    as_of: date,
) -> tuple[OpenInvoice, ...]:
    """Gộp mọi nguồn hóa đơn còn nợ của một phía (`receivable`/`payable`) —
    phase 6 chỉ có nguồn số dư ban đầu, phase 7 nối thêm mà không sửa đây."""
    providers = (
        PROVIDERS.receivable_providers() if side == "receivable" else PROVIDERS.payable_providers()
    )
    merged: list[OpenInvoice] = []
    for provider in providers:
        merged.extend(
            provider.open_invoices(
                session,
                partner_kind=partner_kind,
                partner_id=partner_id,
                branch_id=branch_id,
                as_of=as_of,
            )
        )
    return tuple(merged)


# ---------------------------------------- chênh lệch tỷ giá thu/trả (FR-SYS-066)


def fx_adjustment_lines(
    session: Session,
    voucher: Voucher,
    *,
    money_in: bool,
    settlements: Sequence[SettlementRow],
) -> list[PostingLine]:
    """Cặp dòng VND đưa TK công nợ về giá trị ghi nhận + ghi phần chênh vào
    515/635 cho mỗi dòng đối trừ có `fx_diff ≠ 0`.

    `money_in=True` cho chứng từ thu tiền (phiếu thu, báo có), `False` cho chi
    (phiếu chi, ủy nhiệm chi, séc) — hướng lãi/lỗ phụ thuộc chiều tiền, xem
    `_is_gain`. Lưới 8 kịch bản (tăng/giảm × thu/chi × KH/NCC) ghim ở
    `tests/test_cash_settlement_and_fx.py`.
    """
    with_diff = [row for row in settlements if row.fx_diff != 0]
    if not with_diff:
        return []

    base_currency = _base_currency(session, voucher)
    targets = _find_row_targets(session, with_diff)
    fx_accounts: dict[str, int] = {}

    adjustments: list[PostingLine] = []
    for row in with_diff:
        invoice = targets.get(row.target_id)
        if invoice is None:
            raise PostingValidationError(
                "Chứng từ công nợ được đối trừ không còn tồn tại",
                violations=[
                    PostingViolation(
                        SETTLEMENT_TARGET_MISSING_CODE,
                        "Chứng từ công nợ đã bị xóa hoặc nhập lại — chọn lại "
                        "chứng từ đối trừ trên phiếu",
                        target_id=str(row.target_id),
                    )
                ],
            )
        gain = _is_gain(money_in, row.fx_diff)
        purpose = FX_GAIN_PURPOSE if gain else FX_LOSS_PURPOSE
        if purpose not in fx_accounts:
            fx_accounts[purpose] = _fx_account_id(session, voucher, purpose)
        adjustments.extend(
            _fx_adjustment_pair(
                voucher=voucher,
                money_in=money_in,
                invoice=invoice,
                fx_account_id=fx_accounts[purpose],
                diff=row.fx_diff,
                gain=gain,
                base_currency=base_currency,
            )
        )
    return adjustments


def _is_gain(money_in: bool, fx_diff: Decimal) -> bool:
    """Lãi hay lỗ tỷ giá — phụ thuộc CHIỀU tiền, không chỉ dấu của chênh.

    `fx_diff` = VND theo tỷ giá chứng từ − VND theo tỷ giá ghi nhận. **Thu**:
    nhận nhiều VND hơn giá trị sổ của khoản phải thu → lãi khi dương. **Chi**:
    trả nhiều VND hơn giá trị sổ của khoản phải trả → LỖ khi dương.
    """
    if money_in:
        return fx_diff > 0
    return fx_diff < 0


def _fx_adjustment_pair(
    *,
    voucher: Voucher,
    money_in: bool,
    invoice: OpenInvoice,
    fx_account_id: int,
    diff: Decimal,
    gain: bool,
    base_currency: str,
) -> list[PostingLine]:
    """Bốn hướng (đối chiếu docstring `_is_gain`):

    * Thu, lãi (diff > 0): dòng gốc đã Có 131 theo tỷ giá chứng từ — Có THỪA
      `diff` → Nợ 131 / Có 515.
    * Thu, lỗ (diff < 0): Có 131 THIẾU `|diff|` → Nợ 635 / Có 131.
    * Chi, lỗ (diff > 0): dòng gốc đã Nợ 331 THỪA `diff` → Nợ 635 / Có 331.
    * Chi, lãi (diff < 0): Nợ 331 THIẾU `|diff|` → Nợ 331 / Có 515.
    """
    magnitude = abs(diff)
    # Cả bốn hướng gọn lại thành một luật: LÃI → TK công nợ bên Nợ / 515 bên
    # Có; LỖ → 635 bên Nợ / TK công nợ bên Có.
    invoice_debited = gain
    partner_dimensions = PostingDimensions(
        partner_id=invoice.partner_id, partner_kind=invoice.partner_kind
    )
    description = f"Chênh lệch tỷ giá {'thu' if money_in else 'trả'} tiền — {voucher.voucher_no}"

    def line(
        account_id: int, *, debit: Decimal, credit: Decimal, corresponding: int, on_invoice: bool
    ) -> PostingLine:
        return PostingLine(
            account_id=account_id,
            corresponding_account_id=corresponding,
            debit_fc=debit,
            credit_fc=credit,
            currency=base_currency,
            rate=_ONE,
            dimensions=partner_dimensions if on_invoice else _EMPTY_DIMENSIONS,
            source_line_id=None,
            description=description,
        )

    if invoice_debited:
        return [
            line(
                invoice.account_id,
                debit=magnitude,
                credit=Decimal(0),
                corresponding=fx_account_id,
                on_invoice=True,
            ),
            line(
                fx_account_id,
                debit=Decimal(0),
                credit=magnitude,
                corresponding=invoice.account_id,
                on_invoice=False,
            ),
        ]
    return [
        line(
            fx_account_id,
            debit=magnitude,
            credit=Decimal(0),
            corresponding=invoice.account_id,
            on_invoice=False,
        ),
        line(
            invoice.account_id,
            debit=Decimal(0),
            credit=magnitude,
            corresponding=fx_account_id,
            on_invoice=True,
        ),
    ]


def _find_row_targets(
    session: Session, settlements: Sequence[SettlementRow]
) -> dict[UUID, OpenInvoice]:
    by_kind: dict[int, list[UUID]] = {}
    for row in settlements:
        by_kind.setdefault(row.target_kind, []).append(row.target_id)
    found: dict[UUID, OpenInvoice] = {}
    for kind, target_ids in by_kind.items():
        for invoice in _require_source(kind).find(session, target_ids=target_ids):
            found[invoice.target_id] = invoice
    return found


def _base_currency(session: Session, voucher: Voucher) -> str:
    period = session.get(AccountingPeriod, voucher.period_id)
    year = session.get(FiscalYear, period.fiscal_year_id) if period is not None else None
    if year is None:  # pragma: no cover - FK bảo đảm
        raise RuntimeError(f"Chứng từ {voucher.id} trỏ vào kỳ không có năm tài chính")
    return year.base_currency


def _fx_account_id(session: Session, voucher: Voucher, purpose: str) -> int:
    """TK 515/635 theo cấu hình gói hiệu lực (FR-SYS-066) — thiếu khai là
    khoảng trống cấu hình thật, báo bằng vi phạm ghi sổ chứ không 500."""
    period = session.get(AccountingPeriod, voucher.period_id)
    year = session.get(FiscalYear, period.fiscal_year_id) if period is not None else None
    if year is None:  # pragma: no cover - FK bảo đảm
        raise RuntimeError(f"Chứng từ {voucher.id} trỏ vào kỳ không có năm tài chính")
    package = resolve_package(session, scheme=year.accounting_scheme, on_date=voucher.posting_date)
    entry = default_account(
        session, package_id=package.id, document_type=voucher.document_type, purpose=purpose
    )
    account_id = session.execute(
        select(ChartOfAccount.id).where(
            ChartOfAccount.package_id == package.id,
            ChartOfAccount.code == entry.account_code,
        )
    ).scalar_one_or_none()
    if account_id is None:
        raise PostingValidationError(
            "Gói cấu hình khai TK chênh lệch tỷ giá không tồn tại trong hệ thống TK",
            violations=[
                PostingViolation(
                    FX_ACCOUNT_MISSING_CODE,
                    "TK chênh lệch tỷ giá trong cấu hình không có trong hệ thống "
                    "TK của gói — kiểm tra gói cấu hình",
                    purpose=purpose,
                    account_code=entry.account_code,
                )
            ],
        )
    return account_id
