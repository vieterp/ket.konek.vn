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


class PendingVoucherResponse(BaseModel):
    """Một chứng từ Đã cất chưa ghi sổ — đủ để UI mở đúng chứng từ."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    voucher_no: str
    branch_id: int
    posting_date: date
    period_id: int
    description: str | None


class PendingIssueGroupResponse(BaseModel):
    """Việc còn thiếu của MỘT loại chứng từ (U1): đếm + mẫu, kèm mã hành động.

    `next_action` là mã máy (`post`) chứ không phải nhãn tiếng Việt: nhãn
    "Ghi sổ" thuộc tầng i18n của client, còn mã thì client dùng để dựng đúng
    nút gọi `POST /vouchers/{id}/actions/post`.
    """

    document_type: str
    title: str
    count: int
    next_action: str
    sample: list[PendingVoucherResponse]


class PendingRecalcResponse(BaseModel):
    """Một dấu bẩn trong hàng đợi tính lại — kỳ nhìn thấy số liệu chưa chốt."""

    model_config = ConfigDict(from_attributes=True)

    ledger: int
    branch_id: int
    from_period_id: int
    marked_at: datetime
    reason: str | None


class PendingIssuesResponse(BaseModel):
    """Nguồn của tab "việc còn thiếu" (U1) — phase 4 có hai loại việc:
    chứng từ Đã cất chưa ghi sổ và kỳ đang chờ tính lại số dư."""

    unposted: list[PendingIssueGroupResponse]
    recalc_pending: list[PendingRecalcResponse]
