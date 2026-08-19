"""Hình dạng phản hồi của endpoint chứng từ dùng chung (`/api/v1/vouchers`)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class VoucherResponse(BaseModel):
    """Header chứng từ — đủ cho danh sách và cho phản hồi của hành động."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_type: str
    voucher_no: str
    branch_id: int
    document_date: date
    posting_date: date
    period_id: int
    currency_code: str
    exchange_rate: Decimal
    description: str | None
    status: int
    cashflow_activity: int | None
    created_at: datetime
    created_by: int
    posted_at: datetime | None
    posted_by: int | None
    row_version: int


class VoucherListResponse(BaseModel):
    items: list[VoucherResponse]
    total: int
    page: int
    page_size: int
