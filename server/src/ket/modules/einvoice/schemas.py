"""Thân request / response của phân hệ hóa đơn điện tử (SRS 07, 08).

**Không có schema "sửa hóa đơn".** Đó không phải thiếu sót: BR-EIV-01 nói hóa
đơn đã phát hành là bất biến, và hóa đơn *chưa* phát hành thì không có nội dung
riêng để mà sửa — nội dung nó là chứng từ gốc (xem `models.py` về việc hóa đơn
không mang một con số tiền nào). Đổi ký hiệu của một hóa đơn nháp = xóa nó rồi
lập lại; đổi nội dung = sửa chứng từ bán.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ket.modules.einvoice.models import (
    INVOICE_NO_MAX_LENGTH,
    LOOKUP_CODE_MAX_LENGTH,
    NOTICE_NO_MAX_LENGTH,
    REASON_CODE_MAX_LENGTH,
    TAX_AUTHORITY_CODE_MAX_LENGTH,
    EInvoiceStatus,
    ErrorNoticeKind,
    NoticeStatus,
    RegistrationStatus,
)


class EInvoiceIn(BaseModel):
    """Lập hóa đơn từ một chứng từ gốc (FR-EIV-010)."""

    source_voucher_id: UUID = Field(title="Chứng từ gốc")
    invoice_form_id: int = Field(title="Ký hiệu hóa đơn")


class EInvoiceIssueIn(BaseModel):
    """Phát hành: cấp số cho hóa đơn (FR-EIV-013).

    Chỉ một trường. Mẫu số và ký hiệu đã chốt lúc lập hóa đơn — chọn lại chúng
    ở đây là cho phép đổi ký hiệu của một hóa đơn đã bị từ chối rồi phát hành
    lại, tức để một tờ hóa đơn mang số của hai dãy.
    """

    invoice_date: date = Field(title="Ngày hóa đơn")


class EInvoiceConfirmIn(BaseModel):
    """Kết quả **chấp nhận** từ nhà cung cấp / cơ quan thuế.

    Lát 7D nhận từ người dùng; lát 7E là `outbox` điền, cùng thân này.
    """

    tax_authority_code: str | None = Field(
        default=None, max_length=TAX_AUTHORITY_CODE_MAX_LENGTH, title="Mã cơ quan thuế cấp"
    )
    lookup_code: str | None = Field(
        default=None, max_length=LOOKUP_CODE_MAX_LENGTH, title="Mã tra cứu cho người mua"
    )


class EInvoiceRejectIn(BaseModel):
    """Kết quả **từ chối**. Lý do hiện thẳng lên cột gộp trạng thái (U3)."""

    message: str = Field(min_length=1, title="Lý do từ chối")


class ErrorNoticeIn(BaseModel):
    """Thông báo hủy (CQT) hoặc biên bản hủy (người mua) — FR-EIV-031/032."""

    kind: ErrorNoticeKind = Field(title="Loại văn bản")
    notice_no: str = Field(min_length=1, max_length=NOTICE_NO_MAX_LENGTH, title="Số văn bản")
    notice_date: date = Field(title="Ngày văn bản")
    reason_code: str | None = Field(
        default=None, max_length=REASON_CODE_MAX_LENGTH, title="Mã lý do"
    )
    reason: str | None = Field(default=None, title="Lý do")
    submitted: bool = Field(default=False, title="Đã nộp")


class ErrorNoticeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    einvoice_id: UUID
    kind: ErrorNoticeKind
    notice_no: str
    notice_date: date
    reason_code: str | None
    reason: str | None
    status: NoticeStatus
    submitted_at: datetime | None


class EInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    branch_id: int
    source_voucher_id: UUID
    invoice_form_id: int
    invoice_no: str | None = Field(default=None, max_length=INVOICE_NO_MAX_LENGTH)
    invoice_date: date | None
    status: EInvoiceStatus
    tax_authority_status: int | None
    tax_authority_code: str | None
    tax_authority_message: str | None
    lookup_code: str | None
    replaces_invoice_id: UUID | None
    adjusts_invoice_id: UUID | None
    issued_at: datetime | None


class EInvoiceListOut(BaseModel):
    """Một trang hóa đơn + số đếm theo trạng thái cho bộ lọc U3/FR-EIV-015.

    Số đếm đi kèm danh sách chứ không là một endpoint riêng: màn hình vẽ các
    thẻ lọc ("Chưa phát hành 12 · Phát hành lỗi 2") cùng lúc với lưới, và hai
    lượt gọi cho một màn hình là hai ảnh chụp có thể lệch nhau.

    `counts_by_status` đếm **toàn bộ** phạm vi người gọi, không riêng trang
    đang xem — thẻ lọc nói "còn bao nhiêu việc", không nói "trang này có gì".
    """

    items: tuple[EInvoiceOut, ...]
    total: int
    page: int
    page_size: int
    counts_by_status: dict[int, int]


class InvoiceRegistrationIn(BaseModel):
    """Hồ sơ đăng ký sử dụng HĐĐT / thông báo phát hành (FR-EIV-002, FR-INV-001)."""

    branch_id: int = Field(title="Chi nhánh")
    invoice_form_id: int = Field(title="Ký hiệu hóa đơn")
    start_date: date = Field(title="Ngày bắt đầu sử dụng")
    notice_no: str | None = Field(
        default=None, max_length=NOTICE_NO_MAX_LENGTH, title="Số thông báo phát hành"
    )
    notice_date: date | None = Field(default=None, title="Ngày thông báo")
    quantity: int | None = Field(default=None, ge=1, title="Số lượng hóa đơn")
    range_from: int | None = Field(default=None, ge=1, title="Từ số")
    range_to: int | None = Field(default=None, ge=1, title="Đến số")


class InvoiceRegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    branch_id: int
    invoice_form_id: int
    notice_no: str | None
    notice_date: date | None
    start_date: date
    quantity: int | None
    range_from: int | None
    range_to: int | None
    status: RegistrationStatus
