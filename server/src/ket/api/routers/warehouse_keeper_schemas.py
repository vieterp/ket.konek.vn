"""Hình dạng request/response của hàng đợi + sổ kho thủ kho (SRS 17 §3.2, 8D)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BATCH_SIZE = 200


class KeeperQueueItem(BaseModel):
    """Một phiếu kho chờ thủ kho ghi sổ kho (FR-WHK-010/011)."""

    voucher_id: UUID
    voucher_no: str
    document_type: str
    branch_id: int
    posting_date: date
    warehouse_id: int
    to_warehouse_id: int | None
    delivered_by: str | None
    description: str | None


class KeeperQueueResponse(BaseModel):
    items: tuple[KeeperQueueItem, ...]


class KeeperBookRequest(BaseModel):
    """Ghi sổ kho hàng loạt (FR-WHK-012): theo ngày hạch toán từng phiếu, hoặc
    một ngày tùy chọn áp cho cả lô (kiểm với TỪNG phiếu)."""

    model_config = ConfigDict(extra="forbid")

    voucher_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    """Trần lô: cả lô là MỘT transaction giữ khóa hàng trên `vouchers` suốt lượt
    ghi, nên một danh sách không trần là một transaction không trần. 200 là gấp
    bốn con số FR-WHK-012 nêu ("50 phiếu trong 1 thao tác")."""
    book_date_mode: Literal["posting_date", "custom"] = "posting_date"
    book_date: date | None = None

    @model_validator(mode="after")
    def _date_matches_mode(self) -> KeeperBookRequest:
        if (self.book_date_mode == "custom") != (self.book_date is not None):
            raise ValueError("Ngày tùy chọn phải (và chỉ) đi kèm chế độ ngày tùy chọn")
        return self


class KeeperBookResponse(BaseModel):
    """Cả lô là MỘT transaction nên "đã ghi N dòng sổ kho" là toàn bộ thông
    tin; chi tiết nằm ở sổ kho (client tải lại hàng đợi + sổ sau thao tác)."""

    booked_rows: int


class WarehouseBookRowOut(BaseModel):
    """Một dòng sổ kho (FR-WHK-014) — số lượng, không giá trị."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_id: int
    warehouse_id: int
    item_id: int
    lot_id: int | None
    book_date: date
    voucher_id: UUID
    in_qty: Decimal
    out_qty: Decimal
    posted_by: int
    posted_at: datetime


class WarehouseBookResponse(BaseModel):
    items: tuple[WarehouseBookRowOut, ...]
    total: int
    """Tổng số dòng khớp bộ lọc, trước khi cắt trang."""


class WarehouseCardRow(WarehouseBookRowOut):
    """Một dòng THẺ KHO: dòng sổ kho + tồn lũy kế sau dòng ấy (FR-WHK-014).

    Thẻ kho là sổ của MỘT mã hàng ở MỘT kho, nên tồn lũy kế có nghĩa; sổ kho
    gộp nhiều mã thì không (cộng số lượng khác mã là cộng táo với cam).
    """

    running_qty: Decimal


class WarehouseCardResponse(BaseModel):
    warehouse_id: int
    item_id: int
    opening_qty: Decimal
    """Tồn trước `from_date` — thẻ kho bắt đầu từ một con số, không từ 0."""
    items: tuple[WarehouseCardRow, ...]
    total: int
