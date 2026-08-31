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
chênh vào TK cấu hình `fx_gain`/`fx_loss` (515/635) — từ lát 6C phần này dùng
chung `ket.posting.settlements.fx_adjustment_lines` với chứng từ tiền gửi;
lưới 8 kịch bản (tăng/giảm × thu/chi × KH/NCC) ghim ở
`tests/test_cash_settlement_and_fx.py`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DEPOSIT_ACCOUNT_CODE_PREFIX
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.errors import PostingValidationError, PostingViolation
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
from ket.posting.settlements import (
    FX_ACCOUNT_MISSING_CODE,
    FX_GAIN_PURPOSE,
    FX_LOSS_PURPOSE,
    fx_adjustment_lines,
)

LINE_SIDE_MISSING_CODE = "cash.line_side_missing"

__all__ = [
    "FX_ACCOUNT_MISSING_CODE",
    "FX_GAIN_PURPOSE",
    "FX_LOSS_PURPOSE",
    "LINE_SIDE_MISSING_CODE",
    "build_posting_request",
]

_EMPTY_DIMENSIONS = PostingDimensions()


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

    # Chiều `bank_account` thuộc riêng bên 112x. Luật "dòng không chạm quỹ thì
    # CẢ HAI bên nhận trọn chiều" (xem `_split_pair`) đúng với chiều đối tác/
    # khoản mục, nhưng sai với chiều này: một dòng `Nợ 131 / Có 112` sẽ dán TK
    # ngân hàng lên cả dòng công nợ, và lượt chuyển số dư đầu năm xếp dư phải
    # thu sang nhóm tiền gửi — chi tiết hóa đơn rơi im lặng (review pre-landing
    # H-A). Lọc theo SỐ HIỆU TK, cùng doctrine với mapper ngân hàng.
    touched_ids = [
        account_id
        for line in lines
        for account_id in (line.debit_account_id, line.credit_account_id)
        if account_id is not None
    ]
    accounts = accounts_by_id(session, touched_ids)
    deposit_account_ids = {
        account_id
        for account_id, account in accounts.items()
        if account.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX)
    }

    posting_lines: list[PostingLine] = []
    violations: list[PostingViolation] = []
    for line in lines:
        posting_lines.extend(_split_pair(voucher, body, line, deposit_account_ids, violations))
    if violations:
        raise PostingValidationError(
            "Phiếu còn dòng định khoản thiếu bên Nợ hoặc bên Có", violations=violations
        )

    posting_lines.extend(
        fx_adjustment_lines(
            session,
            voucher,
            money_in=body.kind == CashVoucherKind.RECEIPT,
            settlements=settlements,
        )
    )
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


def _split_pair(
    voucher: Voucher,
    body: CashVoucher,
    line: CashVoucherLine,
    deposit_account_ids: set[int],
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
    # Chiều TK ngân hàng chỉ ở lại bên 112x — xem `build_posting_request`.
    debit_dimensions = _only_deposit_keeps_bank_account(
        debit_dimensions, line.debit_account_id, deposit_account_ids
    )
    credit_dimensions = _only_deposit_keeps_bank_account(
        credit_dimensions, line.credit_account_id, deposit_account_ids
    )

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


def _only_deposit_keeps_bank_account(
    dimensions: PostingDimensions, account_id: int | None, deposit_account_ids: set[int]
) -> PostingDimensions:
    """Bỏ `bank_account_id` khỏi một bên không phải 112x."""
    if dimensions.bank_account_id is None or account_id in deposit_account_ids:
        return dimensions
    return dimensions.model_copy(update={"bank_account_id": None})


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
        bank_account_id=line.bank_account_id,
        extended=extended,
    )
