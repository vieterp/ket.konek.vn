"""Hình dạng response của BFF "Tiền vào tiền ra" (`/api/v1/cashflow/*`, lát 6E-1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

CashflowSource = Literal["cash", "bank"]
"""Nguồn của một thẻ/giao dịch. `cash` = quỹ tiền mặt (TK 111x), `bank` = một
tài khoản ngân hàng doanh nghiệp."""


class CashAccountCard(BaseModel):
    """Thẻ một tài khoản quỹ tiền mặt."""

    source: Literal["cash"] = "cash"
    account_id: int
    account_code: str
    account_name: str
    currency_code: str
    balance: Decimal


class BankAccountCard(BaseModel):
    """Thẻ một tài khoản ngân hàng doanh nghiệp.

    `balance_fc` là số dư NGUYÊN TỆ của chính tài khoản đó (FR-BNK-041 đòi hiện
    đồng thời hai trục); `balance` là số quy đổi để cộng chung một hàng thẻ.
    """

    source: Literal["bank"] = "bank"
    bank_account_id: int
    bank_account_code: str
    bank_account_name: str
    bank_name: str | None
    currency_code: str
    balance_fc: Decimal
    balance: Decimal


class CashflowOverviewResponse(BaseModel):
    """Hàng thẻ của màn hình "Tiền vào tiền ra" (U-Quỹ).

    Thẻ chỉ gồm phần người dùng có quyền XEM: thiếu quyền quỹ thì `cash_accounts`
    rỗng, thiếu quyền ngân hàng thì `bank_accounts` rỗng — không phải 403 cho cả
    màn hình, vì một thủ quỹ chỉ xem quỹ vẫn phải mở được trang. Thiếu CẢ HAI
    mới là 403 (chặn ở dependency của endpoint).

    `unassigned_deposit` = phần số dư TK 112 KHÔNG gắn được tài khoản ngân hàng
    nào (bút toán GLE gõ thẳng vào 112x — xem `bank/balance_service`). Hiện
    thành một con số riêng thay vì bị giấu hoặc bị chia bừa cho các thẻ: tổng
    thẻ ngân hàng + số này = tổng TK 112 trên bảng cân đối, và người dùng nhìn
    ra ngay vì sao hai nơi lệch nhau (ghi chú M-3 của review 6D).
    """

    as_of: date
    ledger: int
    cash_accounts: tuple[CashAccountCard, ...]
    bank_accounts: tuple[BankAccountCard, ...]
    unassigned_deposit: Decimal


class CashflowTransaction(BaseModel):
    """Một dòng trên lưới giao dịch, đã hợp nhất hai module.

    `amount` dương = tiền VÀO, âm = tiền RA — một cột dấu thay vì hai cột
    thu/chi: lưới đổi theo thẻ đang chọn nên chiều luôn xét trên đúng tài khoản
    của thẻ đó.
    """

    voucher_id: UUID
    voucher_no: str
    document_type: str
    source: CashflowSource
    posting_date: date
    document_date: date
    description: str | None
    partner_name: str | None
    amount: Decimal
    status: int


class CashflowTransactionsResponse(BaseModel):
    items: tuple[CashflowTransaction, ...]
    total: int
    """Tổng số dòng khớp bộ lọc — lưới cần biết để vẽ thanh phân trang."""
