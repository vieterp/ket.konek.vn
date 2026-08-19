"""Hình dạng request/response của số dư ban đầu (slice 4C).

Pydantic ở mọi ranh giới API (ADR-015). Báo cáo kiểm/ghi chi tiết nằm trong
`jobs.result` (khuôn H78) — hai response ở đây chỉ trỏ vào job.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ket.posting.engine.models import Ledger


class OpeningValidateResponse(BaseModel):
    """Lượt kiểm đã xếp hàng — báo cáo đọc ở `GET /jobs/{id}` khi job xong."""

    job_id: UUID
    fiscal_year_id: int
    ledger: int
    file_name: str
    content_hash: str


class OpeningCommitRequest(BaseModel):
    """Lượt ghi trỏ vào một lượt kiểm đã đạt (H78/H89)."""

    validation_job_id: UUID
    fiscal_year_id: int = Field(ge=1)
    ledger: int = Field(default=Ledger.FINANCIAL, ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)


class OpeningCommitResponse(BaseModel):
    job_id: UUID


class OpeningBalanceRowResponse(BaseModel):
    """Một dòng số dư ban đầu, kèm mã/tên để màn hình không phải tra thêm."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    detail_kind: int
    account_code: str
    account_name: str
    partner_code: str | None
    partner_name: str | None
    currency_code: str
    exchange_rate: Decimal
    debit_fc: Decimal
    credit_fc: Decimal
    debit: Decimal
    credit: Decimal


class OpeningBalanceListResponse(BaseModel):
    """Trang dòng số dư + hai tổng cân đối của cả phạm vi (FR-OPB-006).

    Hai tổng tính trên **toàn bộ** (năm, sổ) trong phạm vi chi nhánh nhìn thấy,
    không phải trên trang hiện tại — banner "chưa cân" phải đúng kể cả khi
    người dùng đang đứng ở trang 3.
    """

    items: list[OpeningBalanceRowResponse]
    total: int
    page: int
    page_size: int
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


class OpeningInvoiceResponse(BaseModel):
    """Một dòng chi tiết hóa đơn/tạm ứng của một dòng số dư (FR-OPB-003)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_no: str | None
    invoice_date: date | None
    due_date: date | None
    amount_fc: Decimal
    amount: Decimal
    paid_amount: Decimal


class OpeningInvoiceListResponse(BaseModel):
    items: list[OpeningInvoiceResponse]
