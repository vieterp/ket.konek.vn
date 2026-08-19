"""Hình dạng phản hồi của endpoint sổ cái (`/api/v1/ledger`)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class TrialBalanceRowResponse(BaseModel):
    """Một tài khoản trên bảng cân đối: dư đầu / phát sinh / dư cuối."""

    model_config = ConfigDict(from_attributes=True)

    account_id: int
    account_code: str
    account_name: str
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


class TrialBalanceResponse(BaseModel):
    """Bảng cân đối tài khoản của một (kỳ, sổ).

    `stale=True`: kỳ còn chờ tính lại snapshot — số vừa tính thẳng từ sổ cái
    (đúng nhưng chậm hơn); UI hiện chỉ báo "đang chờ tính lại" và gợi ý chạy
    tác vụ `posting.balances.recalc`.
    """

    ledger: int
    period_id: int
    branch_id: int | None
    stale: bool
    rows: list[TrialBalanceRowResponse]
