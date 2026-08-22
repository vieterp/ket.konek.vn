"""Hình dạng request/response của hàng đợi + sổ quỹ thủ quỹ (SRS 17, lát 6C)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.protocols import TreasurerPendingVoucher


class TreasurerQueueItem(BaseModel):
    """Một phiếu chờ ghi sổ quỹ — chép phẳng từ `kernel.protocols
    .TreasurerPendingVoucher` (hợp đồng API tách khỏi hợp đồng nội bộ)."""

    voucher_id: UUID
    voucher_no: str
    document_type: str
    branch_id: int
    posting_date: date
    cash_account_id: int
    is_receipt: bool
    amount: Decimal
    payer_receiver_name: str | None
    description: str | None

    @classmethod
    def from_pending(cls, pending: TreasurerPendingVoucher) -> TreasurerQueueItem:
        return cls(**pending.model_dump())


class TreasurerQueueResponse(BaseModel):
    items: tuple[TreasurerQueueItem, ...]


class TreasurerBookRequest(BaseModel):
    """Ghi sổ quỹ hàng loạt (FR-WHK-002/003): chọn ngày theo chứng từ hoặc một
    ngày tùy chọn áp cho cả lô (BR-WHK-05 kiểm với TỪNG phiếu)."""

    model_config = ConfigDict(extra="forbid")

    voucher_ids: tuple[UUID, ...] = Field(min_length=1)
    book_date_mode: Literal["posting_date", "custom"] = "posting_date"
    book_date: date | None = None

    @model_validator(mode="after")
    def _date_matches_mode(self) -> TreasurerBookRequest:
        if (self.book_date_mode == "custom") != (self.book_date is not None):
            raise ValueError("Ngày tùy chọn phải (và chỉ) đi kèm chế độ ngày tùy chọn")
        return self


class TreasurerBookResponse(BaseModel):
    """Kết quả ghi sổ hàng loạt — chỉ đếm: cả lô là MỘT transaction nên "đã ghi
    N phiếu" là toàn bộ thông tin; chi tiết dòng nằm ở sổ quỹ (client tải lại
    hàng đợi + sổ sau thao tác). Giữ mỏng còn vì tham chiếu idempotency chỉ
    chứa được một khóa ngắn, không chứa được danh sách phiếu."""

    booked_count: int


class TreasurerCashBookRowOut(BaseModel):
    """Một dòng sổ quỹ tiền mặt của thủ quỹ (FR-WHK-005)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_id: int
    cash_account_id: int
    book_date: date
    voucher_id: UUID
    receipt_amount: Decimal
    payment_amount: Decimal
    posted_by: int
    posted_at: datetime


class TreasurerCashBookResponse(BaseModel):
    items: tuple[TreasurerCashBookRowOut, ...]
    total: int
    """Tổng số dòng khớp bộ lọc, trước khi cắt trang — thanh phân trang của
    màn hình thủ quỹ (U6) cần biết còn bao nhiêu trang phía sau."""
