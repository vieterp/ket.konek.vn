"""Dịch chứng từ tiền gửi → `PostingRequest` (hợp đồng một chiều với engine).

Cùng khuôn `cash_book/posting_mapper.py`: dòng là cặp Nợ/Có + một số tiền,
mapper trải thành hai `PostingLine` một bên, hai sổ ghi giống nhau
(`management_lines=None`, LD-07).

Chiều phân tích gắn vào **bên nghiệp vụ** — nhưng khác phiếu quỹ, thân chứng
từ tiền gửi không mang TK kế toán (nó mang tài khoản ngân hàng DANH MỤC,
FR-BNK-002; TK 112x nằm trên dòng). Bên tiền nhận diện theo **số hiệu TK nhóm
tiền** (`MONEY_ACCOUNT_CODE_PREFIXES` — cùng doctrine với
`CASH_ACCOUNT_CODE_PREFIXES` của guard 6B: nhóm 111/112 = tiền là bất biến
SRS-định-nghĩa của cả hai chế độ kế toán, không phải đích cấu hình): bên tiền
không nhận chiều khi bên kia là bên nghiệp vụ; dòng cả hai bên cùng là tiền
(chuyển nội bộ 112↔112) hay không bên nào là tiền thì cả hai cùng nhận —
đúng luật của phiếu quỹ, chỉ khác cách nhận diện bên tiền.

Chênh lệch tỷ giá thu/trả tiền (FR-SYS-066/FR-BNK-006): dùng chung
`ket.posting.settlements.fx_adjustment_lines` với phiếu quỹ, chiều tiền theo
`MONEY_IN_BY_KIND`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.modules.bank.models import (
    BankSettlement,
    BankVoucher,
    BankVoucherKind,
    BankVoucherLine,
)
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PartnerKind,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)
from ket.posting.settlements import fx_adjustment_lines

LINE_SIDE_MISSING_CODE = "bank.line_side_missing"

_EMPTY_DIMENSIONS = PostingDimensions()

_MONEY_IN_BY_KIND = {
    BankVoucherKind.CREDIT_ADVICE: True,
    BankVoucherKind.PAYMENT_ORDER: False,
    BankVoucherKind.CHEQUE: False,
}

MONEY_ACCOUNT_CODE_PREFIXES = ("111", "112")
"""Nhóm TK tiền — bên không nhận chiều phân tích. Literal số hiệu CÓ CHỦ ĐÍCH,
cùng lập luận `CASH_ACCOUNT_CODE_PREFIXES` (cash_book/guards.py, không import
được vì luật module #1): nhóm 111/112 = tiền do chính SRS định nghĩa và là bất
biến chung của TT99 lẫn TT133; chế độ tương lai đổi nhóm TK tiền thì chỗ sửa
là MỘT hằng này."""

DEPOSIT_ACCOUNT_CODE_PREFIX = MONEY_ACCOUNT_CODE_PREFIXES[1]
"""Nhóm 112 — bên mang chiều `bank_account`."""


def _deposit_owner(body: BankVoucher, *, debit_side: bool) -> int:
    """TK ngân hàng doanh nghiệp sở hữu một bên 112x của chứng từ tiền gửi.

    BC/UNC/SEC: mọi bên 112x thuộc `bank_account_id` của thân. Chuyển nội bộ:
    bên **Nợ** là tài khoản ĐÍCH (`counter_bank_account_id`), bên **Có** là tài
    khoản nguồn — một chứng từ đứng trên sổ của cả hai tài khoản, mỗi bên một
    chiều.

    Trước lát 6G-1 luật này sống ở đường ĐỌC (`bank/balance_service`) và được
    chép lại ba lần bằng SQL, nên chứng từ quỹ và bút toán tổng hợp chạm 112 —
    không có thân `bank_vouchers` để suy — rơi ra ngoài mọi báo cáo tiền gửi.
    Nay nó ở đường GHI: engine lưu kết quả vào `gl_postings.bank_account_id` và
    mọi người đọc chỉ còn đọc cột.
    """
    if body.kind == BankVoucherKind.INTERNAL_TRANSFER and debit_side:
        counter = body.counter_bank_account_id
        if counter is None:  # pragma: no cover - service đòi cặp TK khi tạo CTNB
            raise RuntimeError("Chuyển tiền nội bộ thiếu tài khoản ngân hàng đích")
        return counter
    return body.bank_account_id


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Đọc chi tiết đã lưu và dựng yêu cầu ghi sổ — callable đăng ký vào
    `POSTING_DOCUMENT_REGISTRY` cho cả bốn loại chứng từ tiền gửi."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(BankVoucher, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
        raise RuntimeError(f"Chứng từ tiền gửi {voucher_id} thiếu header hoặc thân")
    lines = (
        session.execute(
            select(BankVoucherLine)
            .where(BankVoucherLine.voucher_id == voucher_id)
            .order_by(BankVoucherLine.line_no)
        )
        .scalars()
        .all()
    )
    settlements = (
        session.execute(select(BankSettlement).where(BankSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )

    touched_ids = [
        account_id
        for line in lines
        for account_id in (line.debit_account_id, line.credit_account_id)
        if account_id is not None
    ]
    accounts = accounts_by_id(session, touched_ids)
    money_account_ids = {
        account_id
        for account_id, account in accounts.items()
        if account.code.startswith(MONEY_ACCOUNT_CODE_PREFIXES)
    }
    deposit_account_ids = {
        account_id
        for account_id, account in accounts.items()
        if account.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX)
    }

    posting_lines: list[PostingLine] = []
    violations: list[PostingViolation] = []
    for line in lines:
        posting_lines.extend(
            _split_pair(voucher, body, line, money_account_ids, deposit_account_ids, violations)
        )
    if violations:
        raise PostingValidationError(
            "Chứng từ còn dòng định khoản thiếu bên Nợ hoặc bên Có", violations=violations
        )

    money_in = _MONEY_IN_BY_KIND.get(body.kind)
    if money_in is not None and settlements:
        posting_lines.extend(
            fx_adjustment_lines(session, voucher, money_in=money_in, settlements=settlements)
        )
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


def _split_pair(
    voucher: Voucher,
    body: BankVoucher,
    line: BankVoucherLine,
    money_account_ids: set[int],
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
    debit_is_money = line.debit_account_id in money_account_ids
    credit_is_money = line.credit_account_id in money_account_ids
    # Bên tiền không nhận chiều; dòng không chạm (hoặc hai bên cùng chạm) TK
    # tiền thì cả hai bên cùng nhận — đúng luật phiếu quỹ (xem docstring).
    debit_dimensions = _EMPTY_DIMENSIONS if debit_is_money and not credit_is_money else dimensions
    credit_dimensions = _EMPTY_DIMENSIONS if credit_is_money and not debit_is_money else dimensions

    # Chiều `bank_account` đi ngược luật trên: nó thuộc CHÍNH bên 112x, kể cả
    # khi bên đó là bên tiền không nhận chiều nào khác. Thiếu nó thì validator
    # `dimension_required` chặn ngay ở chứng từ tiền gửi — nơi câu trả lời đã
    # nằm sẵn trên thân chứng từ.
    if line.debit_account_id in deposit_account_ids:
        debit_dimensions = debit_dimensions.model_copy(
            update={"bank_account_id": _deposit_owner(body, debit_side=True)}
        )
    if line.credit_account_id in deposit_account_ids:
        credit_dimensions = credit_dimensions.model_copy(
            update={"bank_account_id": _deposit_owner(body, debit_side=False)}
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


def _line_dimensions(line: BankVoucherLine) -> PostingDimensions:
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
