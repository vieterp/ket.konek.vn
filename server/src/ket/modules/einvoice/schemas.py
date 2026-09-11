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

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ket.modules.einvoice.models import (
    BASE_URL_MAX_LENGTH,
    INVOICE_NO_MAX_LENGTH,
    LEGAL_BASIS_MAX_LENGTH,
    LOOKUP_CODE_MAX_LENGTH,
    NOTICE_NO_MAX_LENGTH,
    PROVIDER_CODE_MAX_LENGTH,
    REASON_CODE_MAX_LENGTH,
    SENT_TO_MAX_LENGTH,
    TAX_AUTHORITY_CODE_MAX_LENGTH,
    TAX_CODE_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    EInvoiceStatus,
    ErrorKind,
    ErrorNoticeKind,
    NoticeStatus,
    OutboxOperation,
    OutboxStatus,
    RegistrationStatus,
    Remedy,
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


class ErrorFlowOut(BaseModel):
    """Một nhánh của bảng quyết định xử lý sai sót (`docs/srs/07` §4.4)."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    error_kind: ErrorKind
    buyer_declared: bool | None = Field(
        default=None,
        title="Khách đã kê khai",
        description=(
            "`null` = nhánh này không hỏi câu ấy, không phải 'chưa biết' — wizard "
            "dừng sau câu hỏi thứ nhất."
        ),
    )
    remedy: Remedy
    legal_basis: str = Field(max_length=LEGAL_BASIS_MAX_LENGTH, title="Căn cứ pháp lý")


class ResolveErrorIn(BaseModel):
    """Câu trả lời của kế toán cho wizard xử lý sai sót (FR-EIV-030..034)."""

    error_kind: ErrorKind = Field(title="Hóa đơn sai chỗ nào")
    buyer_declared: bool | None = Field(default=None, title="Khách đã kê khai thuế chưa")
    notice_no: str = Field(
        min_length=1, max_length=NOTICE_NO_MAX_LENGTH, title="Số thông báo sai sót"
    )
    notice_date: date = Field(title="Ngày thông báo sai sót")
    reason: str | None = Field(default=None, title="Diễn giải sai sót")


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
    sent_at: datetime | None
    sent_to: str | None


class ResolveErrorOut(BaseModel):
    """Cách xử lý đã chọn và những gì hệ thống vừa dựng."""

    remedy: Remedy
    flow_code: str
    legal_basis: str
    notice: ErrorNoticeOut
    replacement: EInvoiceOut | None = Field(
        default=None,
        title="Hóa đơn thay thế",
        description="Chỉ có ở cách xử lý THAY THẾ; tờ nháp mới trên chính chứng từ cũ.",
    )


class MarkSentIn(BaseModel):
    """Lời khai "đã gửi bản thể hiện cho người mua" (FR-EIV-020, cạnh `SEND`).

    Lượt gửi xảy ra **ngoài phần mềm** (quyết định user 2026-09-08), nên đây là
    một bản ghi nhận chứ không phải lệnh gửi — xem `service.mark_sent`.
    """

    sent_to: str = Field(
        title="Đã gửi cho",
        min_length=1,
        max_length=SENT_TO_MAX_LENGTH,
        description=(
            "Địa chỉ thư người nhận, hoặc mô tả cách giao. Nhiều địa chỉ ngăn "
            "bằng dấu chấm phẩy (FR-EIV-022)."
        ),
    )
    sent_at: AwareDatetime | None = Field(
        default=None,
        title="Thời điểm gửi",
        description=(
            "Bỏ trống = bây giờ. Nhận ngày lùi vì lượt gửi có thể đã xảy ra "
            "trước đó; không nhận thời điểm ở tương lai hay trước lúc phát hành. "
            "Phải kèm múi giờ (ISO 8601 có hậu tố `Z` hoặc `+07:00`)."
        ),
    )
    """`AwareDatetime`, không `datetime` trần — và đây là bản sửa của một lỗi 500.

    Đây là trường **datetime do client gửi lên đầu tiên của cả dự án** (mọi
    `datetime` khác trong `schemas` là dữ liệu ĐI RA: `created_at`, `issued_at`),
    nên không quy ước sẵn nào đỡ hộ. Một giá trị không múi giờ đi tới
    `service.mark_sent` rồi đem so với `datetime.now(UTC)` là `TypeError` — tức
    `500 "lỗi không mong muốn"` cho một đầu vào người dùng gõ sai. Bắt ở schema
    thì nó thành `422` kèm câu nói rõ thiếu gì."""


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


class OutboxRowOut(BaseModel):
    """Một dòng hàng đợi truyền tải, cho panel vận hành (7E-1).

    **Không có `client_ref`.** Nó là khóa chống trùng dùng với nhà cung cấp;
    lộ ra API là mời một client tự dựng lượt gửi mang đúng khóa ấy, tức đúng
    đường mà `UNIQUE (client_ref)` sinh ra để đóng. Người vận hành cần biết
    *chặng nào* và *hỏng ở đâu*, không cần con số ấy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    einvoice_id: UUID
    branch_id: int
    provider_code: str
    operation: OutboxOperation
    provider_ref: str | None
    status: OutboxStatus
    attempt_count: int
    next_attempt_at: datetime | None
    last_error: str | None
    created_at: datetime


class OutboxListOut(BaseModel):
    """Một trang hàng đợi, kèm số dòng đang tới hạn để panel hiện được ngay."""

    items: list[OutboxRowOut]
    total: int
    due_now: int


class ProviderProfileIn(BaseModel):
    """Hồ sơ đăng nhập một nhà cung cấp hóa đơn điện tử (FR-EIV-001).

    Mật khẩu chỉ đi **vào**: không schema nào trả nó ra, và cột lưu là `bytea`
    đã mã hóa (xem `EInvoiceProviderProfile`).
    """

    provider_code: str = Field(max_length=PROVIDER_CODE_MAX_LENGTH, title="Mã nhà cung cấp")
    base_url: str = Field(max_length=BASE_URL_MAX_LENGTH, title="Địa chỉ máy chủ")
    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH, title="Tên đăng nhập")
    password: str = Field(min_length=1, title="Mật khẩu")
    tax_code: str = Field(min_length=1, max_length=TAX_CODE_MAX_LENGTH, title="Mã số thuế")
    is_active: bool = Field(default=True, title="Đang dùng")


class ProviderProfileOut(BaseModel):
    """Hồ sơ đọc ra — **không có mật khẩu**, kể cả dạng đã mã hóa.

    Trả ciphertext ra API là biến một bí mật thành thứ ai đọc được response cũng
    cầm được; khóa mã hóa thì nằm ở máy chủ, nhưng bản mã vẫn là thứ mang đi thử
    ngoại tuyến được.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_code: str
    base_url: str
    username: str
    tax_code: str
    is_active: bool
