"""Dịch phiếu thu/chi → `PostingRequest` (hợp đồng một chiều với engine).

Dòng phiếu là **cặp Nợ/Có + một số tiền** (cách kế toán viên đọc); engine nhận
dòng **một bên**. Mapper trải mỗi cặp thành hai `PostingLine` cùng số tiền,
TK bên này làm đối ứng của bên kia — hai sổ ghi giống nhau nên
`management_lines=None` (engine tự nhân bản, LD-07).

Chiều phân tích gắn vào **bên nghiệp vụ** (bên không phải TK quỹ của phiếu):
phiếu thu ghi Nợ 1111/Có 131 thì đối tác thuộc dòng 131 — gắn cả hai bên sẽ
đổ chiều đối tác lên TK quỹ và thẻ công nợ phase 7 đọc `gl_postings` sẽ đếm
đôi. Dòng không chạm TK quỹ (định khoản tự do) gắn cả hai bên: không có cơ sở
nào để chọn, và thừa chiều trên TK không theo dõi là vô hại còn thiếu thì
validator chặn.

Chênh lệch tỷ giá thu/trả tiền (FR-SYS-066): mỗi dòng đối trừ có `fx_diff ≠ 0`
sinh thêm một cặp dòng VND đưa TK công nợ về đúng giá trị ghi nhận và đẩy phần
chênh vào TK cấu hình `fx_gain`/`fx_loss` (515/635). Bốn hướng dấu — xem
`_fx_adjustment_pair`; lưới 8 kịch bản (tăng/giảm × thu/chi × KH/NCC) ghim ở
`tests/modules/cash_book/test_fx_difference.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.accounts_provider import default_account, resolve_package
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.protocols import PROVIDERS, OpenInvoice, SettlementTargetKind
from ket.modules.cash_book.models import (
    CashSettlement,
    CashVoucher,
    CashVoucherKind,
    CashVoucherLine,
)
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PartnerKind,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)

LINE_SIDE_MISSING_CODE = "cash.line_side_missing"
FX_ACCOUNT_MISSING_CODE = "cash.fx_account_missing"

FX_GAIN_PURPOSE = "fx_gain"
FX_LOSS_PURPOSE = "fx_loss"

_EMPTY_DIMENSIONS = PostingDimensions()
_ONE = Decimal(1)


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Đọc chi tiết đã lưu và dựng yêu cầu ghi sổ — callable đăng ký vào
    `POSTING_DOCUMENT_REGISTRY` cho cả PT lẫn PC."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(CashVoucher, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
        raise RuntimeError(f"Phiếu thu/chi {voucher_id} thiếu header hoặc thân")
    lines = (
        session.execute(
            select(CashVoucherLine)
            .where(CashVoucherLine.voucher_id == voucher_id)
            .order_by(CashVoucherLine.line_no)
        )
        .scalars()
        .all()
    )
    settlements = (
        session.execute(select(CashSettlement).where(CashSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )

    posting_lines: list[PostingLine] = []
    violations: list[PostingViolation] = []
    for line in lines:
        posting_lines.extend(_split_pair(voucher, body, line, violations))
    if violations:
        raise PostingValidationError(
            "Phiếu còn dòng định khoản thiếu bên Nợ hoặc bên Có", violations=violations
        )

    posting_lines.extend(_fx_adjustment_lines(session, voucher, body, settlements))
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


def _split_pair(
    voucher: Voucher,
    body: CashVoucher,
    line: CashVoucherLine,
    violations: list[PostingViolation],
) -> list[PostingLine]:
    if line.debit_account_id is None or line.credit_account_id is None:
        violations.append(
            PostingViolation(
                LINE_SIDE_MISSING_CODE,
                "Dòng định khoản phải đủ cả TK Nợ và TK Có trước khi ghi sổ",
                line_no=line.line_no,
            )
        )
        return []

    dimensions = _line_dimensions(line)
    debit_is_cash = line.debit_account_id == body.cash_account_id
    credit_is_cash = line.credit_account_id == body.cash_account_id
    # Bên quỹ không nhận chiều; dòng không chạm quỹ thì cả hai bên cùng nhận.
    debit_dimensions = _EMPTY_DIMENSIONS if debit_is_cash and not credit_is_cash else dimensions
    credit_dimensions = _EMPTY_DIMENSIONS if credit_is_cash and not debit_is_cash else dimensions

    description = line.description or voucher.description
    return [
        PostingLine(
            account_id=line.debit_account_id,
            corresponding_account_id=line.credit_account_id,
            debit_fc=line.amount_fc,
            credit_fc=Decimal(0),
            currency=voucher.currency_code,
            rate=voucher.exchange_rate,
            dimensions=debit_dimensions,
            source_line_id=line.id,
            description=description,
        ),
        PostingLine(
            account_id=line.credit_account_id,
            corresponding_account_id=line.debit_account_id,
            debit_fc=Decimal(0),
            credit_fc=line.amount_fc,
            currency=voucher.currency_code,
            rate=voucher.exchange_rate,
            dimensions=credit_dimensions,
            source_line_id=line.id,
            description=description,
        ),
    ]


def _line_dimensions(line: CashVoucherLine) -> PostingDimensions:
    extended = tuple(
        ExtendedDimensionValue(dimension_id=int(dimension_id), value_id=value_id)
        for dimension_id, value_id in sorted((line.extended_dimensions or {}).items())
    )
    return PostingDimensions(
        partner_id=line.partner_id,
        partner_kind=(PartnerKind(line.partner_kind) if line.partner_kind is not None else None),
        cost_object_id=line.cost_object_id,
        project_id=line.project_id,
        order_id=line.order_id,
        contract_id=line.contract_id,
        expense_item_id=line.expense_item_id,
        item_id=line.item_id,
        warehouse_id=line.warehouse_id,
        extended=extended,
    )


def _fx_adjustment_lines(
    session: Session,
    voucher: Voucher,
    body: CashVoucher,
    settlements: Sequence[CashSettlement],
) -> list[PostingLine]:
    with_diff = [row for row in settlements if row.fx_diff != 0]
    if not with_diff:
        return []

    base_currency = _base_currency(session, voucher)
    targets = _find_targets(session, with_diff)
    fx_accounts: dict[str, int] = {}

    adjustments: list[PostingLine] = []
    for row in with_diff:
        invoice = targets.get(row.target_id)
        if invoice is None:
            raise PostingValidationError(
                "Chứng từ công nợ được đối trừ không còn tồn tại",
                violations=[
                    PostingViolation(
                        "settlement.target_missing",
                        "Chứng từ công nợ đã bị xóa hoặc nhập lại — chọn lại "
                        "chứng từ đối trừ trên phiếu",
                        target_id=str(row.target_id),
                    )
                ],
            )
        gain = _is_gain(body.kind, row.fx_diff)
        purpose = FX_GAIN_PURPOSE if gain else FX_LOSS_PURPOSE
        if purpose not in fx_accounts:
            fx_accounts[purpose] = _fx_account_id(session, voucher, purpose)
        adjustments.extend(
            _fx_adjustment_pair(
                voucher=voucher,
                body=body,
                invoice=invoice,
                fx_account_id=fx_accounts[purpose],
                diff=row.fx_diff,
                gain=gain,
                base_currency=base_currency,
            )
        )
    return adjustments


def _is_gain(kind: int, fx_diff: Decimal) -> bool:
    """Lãi hay lỗ tỷ giá — phụ thuộc CHIỀU tiền, không chỉ dấu của chênh.

    `fx_diff` = VND theo tỷ giá phiếu − VND theo tỷ giá ghi nhận. Phiếu **thu**:
    nhận nhiều VND hơn giá trị sổ của khoản phải thu → lãi khi dương. Phiếu
    **chi**: trả nhiều VND hơn giá trị sổ của khoản phải trả → LỖ khi dương.
    """
    if kind == CashVoucherKind.RECEIPT:
        return fx_diff > 0
    return fx_diff < 0


def _fx_adjustment_pair(
    *,
    voucher: Voucher,
    body: CashVoucher,
    invoice: OpenInvoice,
    fx_account_id: int,
    diff: Decimal,
    gain: bool,
    base_currency: str,
) -> list[PostingLine]:
    """Cặp dòng VND đưa TK công nợ về giá trị ghi nhận + ghi phần chênh vào
    515/635. Bốn hướng:

    * Thu, lãi (diff > 0): dòng gốc đã Có 131 theo tỷ giá phiếu — Có THỪA
      `diff` → Nợ 131 / Có 515.
    * Thu, lỗ (diff < 0): Có 131 THIẾU `|diff|` → Nợ 635 / Có 131.
    * Chi, lỗ (diff > 0): dòng gốc đã Nợ 331 THỪA `diff` → Nợ 635 / Có 331.
    * Chi, lãi (diff < 0): Nợ 331 THIẾU `|diff|` → Nợ 331 / Có 515.
    """
    magnitude = abs(diff)
    receipt = body.kind == CashVoucherKind.RECEIPT
    # Cả bốn hướng gọn lại thành một luật: LÃI → TK công nợ bên Nợ / 515 bên
    # Có; LỖ → 635 bên Nợ / TK công nợ bên Có (đối chiếu bốn dòng ở docstring).
    invoice_debited = gain
    partner_dimensions = PostingDimensions(
        partner_id=invoice.partner_id, partner_kind=invoice.partner_kind
    )
    description = f"Chênh lệch tỷ giá {'thu' if receipt else 'trả'} tiền — {voucher.voucher_no}"

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


def _find_targets(session: Session, settlements: list[CashSettlement]) -> dict[UUID, OpenInvoice]:
    by_kind: dict[int, list[UUID]] = {}
    for row in settlements:
        by_kind.setdefault(row.target_kind, []).append(row.target_id)
    found: dict[UUID, OpenInvoice] = {}
    for kind, target_ids in by_kind.items():
        source = PROVIDERS.settlement_source(SettlementTargetKind(kind))
        if source is None:
            raise RuntimeError(f"Không có SettlementTargetSource cho target_kind={kind}")
        for invoice in source.find(session, target_ids=target_ids):
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
