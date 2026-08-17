"""Đối tác — **một** danh mục cho cả khách hàng lẫn nhà cung cấp (FR-SYS-031).

Hai danh mục tách rời là cách phổ biến và là cách sai: rất nhiều đơn vị vừa mua
vừa bán với cùng một pháp nhân, và hai bản ghi cho một mã số thuế nghĩa là hai
số dư công nợ không bao giờ bù trừ được, hai địa chỉ trôi lệch nhau, và một lần
đổi tên phải nhớ sửa hai chỗ. Ở đây là một bản ghi mang hai cờ `is_customer` /
`is_vendor`; hai danh sách của giao diện là hai bộ lọc trên cùng bảng
(`CatalogFlag`), không phải hai bảng.

Nhóm khách hàng / nhóm nhà cung cấp (SRS §5.1 "Nhóm KH/NCC") dùng chính cây của
`MasterDataRow` — `parent_id` + `is_group`, không thêm cột nào.

**Không** có ở lát này, có chủ đích:

* Tra tên/địa chỉ theo mã số thuế và kiểm tình trạng hoạt động (FR-SYS-030/034,
  cả hai SHOULD): chúng gọi dịch vụ của cơ quan thuế qua internet, mà bản cài
  đích chạy trong mạng nội bộ và phải chạy được khi mất mạng (LD-01). Cột
  `tax_code` có sẵn nên thêm đường tra cứu sau không phải migrate gì.
* Số dư công nợ và hạn mức đang dùng tới đâu: `ar_ap_ledger` thuộc module
  `receivables` của phase 7 (RT-18). Ở đây chỉ lưu **ngưỡng**; nơi so ngưỡng với
  số thật nằm cùng chỗ với số thật.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import (
    NAME_MAX_LENGTH,
    MasterDataRow,
    master_data_table_args,
)
from ket.kernel.money import MONEY_SCALE_MAX

PARTNER_TABLE_NAME = "partners"

TAX_CODE_MAX_LENGTH = 20
"""Mã số thuế Việt Nam là 10 hoặc 13 ký tự (13 khi có mã đơn vị phụ thuộc); trần
20 còn chỗ cho mã nước ngoài của đối tác không cư trú."""

ADDRESS_MAX_LENGTH = 500
PHONE_MAX_LENGTH = 50
EMAIL_MAX_LENGTH = 255
WEBSITE_MAX_LENGTH = 255
REGION_MAX_LENGTH = 100

CREDIT_LIMIT_PRECISION = 20
CREDIT_LIMIT_SCALE = MONEY_SCALE_MAX
"""Ngưỡng nợ giữ đúng số chữ số thập phân **tối đa** mà tùy chọn `money.scale`
cho phép. Lưu hẹp hơn thì một bản cài đặt `money.scale = 6` sẽ thấy ngưỡng của
mình bị làm tròn lúc ghi — một cái bẫy im lặng ngay trên con số dùng để chặn bán
chịu."""


def _partner_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(PARTNER_TABLE_NAME),
        # Một đối tác không phải khách cũng không phải nhà cung cấp thì không có
        # màn hình nào hiển thị nó và không chứng từ nào chọn được nó — bản ghi
        # đó chỉ tồn tại để làm nhiễu ô tra cứu. Nút **nhóm** thì được miễn: nó
        # chỉ để gom cây, không bao giờ lên chứng từ.
        CheckConstraint(
            "is_group OR is_customer OR is_vendor",
            name="partner_is_customer_or_vendor",
        ),
        CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0", name="credit_limit_not_negative"
        ),
        CheckConstraint("tax_code IS NULL OR tax_code <> ''", name="tax_code_not_blank"),
        # Mã số thuế **không** duy nhất bằng ràng buộc: hộ kinh doanh và cá nhân
        # không có MST, chi nhánh hạch toán phụ thuộc dùng chung MST của trụ sở,
        # và dữ liệu chuyển từ phần mềm cũ sang thường có trùng thật cần người
        # dùng tự gộp (FR-SYS-016). Chỉ mục thường để màn hình tra được nhanh và
        # để cảnh báo trùng lúc nhập.
        Index(f"ix_{PARTNER_TABLE_NAME}_tax_code", "tax_code"),
        Index(f"ix_{PARTNER_TABLE_NAME}_payment_term_id", "payment_term_id"),
    )


class Partner(MasterDataRow):
    """Một khách hàng, một nhà cung cấp, hoặc cùng lúc cả hai."""

    __tablename__ = PARTNER_TABLE_NAME
    __table_args__ = _partner_table_args()

    is_customer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_vendor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    is_organization: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """Tổ chức hay cá nhân (SRS §5.1). Quyết định mẫu hóa đơn điền "Tên đơn vị"
    hay "Họ tên người mua" ở phase 7, và cách khai trên bảng kê thuế."""

    tax_code: Mapped[str | None] = mapped_column(String(TAX_CODE_MAX_LENGTH), nullable=True)

    address: Mapped[str | None] = mapped_column(String(ADDRESS_MAX_LENGTH), nullable=True)
    country: Mapped[str | None] = mapped_column(String(REGION_MAX_LENGTH), nullable=True)
    province: Mapped[str | None] = mapped_column(String(REGION_MAX_LENGTH), nullable=True)
    district: Mapped[str | None] = mapped_column(String(REGION_MAX_LENGTH), nullable=True)

    contact_name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(PHONE_MAX_LENGTH), nullable=True)
    email: Mapped[str | None] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=True)
    website: Mapped[str | None] = mapped_column(String(WEBSITE_MAX_LENGTH), nullable=True)

    invoice_recipient: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    invoice_email: Mapped[str | None] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=True)
    """Người và hộp thư **nhận hóa đơn điện tử**, tách khỏi người liên hệ chung:
    ở phần lớn doanh nghiệp đó là hai người khác nhau, và gửi nhầm địa chỉ nghĩa
    là hóa đơn coi như chưa tới (phase 7)."""

    payment_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_terms.id", ondelete="RESTRICT"), nullable=True
    )
    """Điều khoản thanh toán mặc định — nguồn tính hạn nợ của chứng từ (FR-SYS-032).

    Số ngày được nợ nằm ở `payment_terms.due_days` chứ **không** lặp thêm một
    cột ở đây: hai chỗ giữ cùng một con số là hai chỗ để chúng lệch nhau, và khi
    lệch thì không ai biết chỗ nào đúng."""

    credit_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(CREDIT_LIMIT_PRECISION, CREDIT_LIMIT_SCALE), nullable=True
    )
    """Ngưỡng nợ tối đa; `NULL` = không đặt ngưỡng (FR-SYS-032). Đơn vị là đồng
    tiền hạch toán — cảnh báo ở phase 7 so nó với số dư đã quy đổi."""


class PartnerFields(BaseModel):
    """Phần riêng của đối tác trên API (`registry.CatalogSpec`)."""

    is_customer: bool = False
    is_vendor: bool = False
    is_organization: bool = True

    tax_code: str | None = Field(default=None, max_length=TAX_CODE_MAX_LENGTH)
    address: str | None = Field(default=None, max_length=ADDRESS_MAX_LENGTH)
    country: str | None = Field(default=None, max_length=REGION_MAX_LENGTH)
    province: str | None = Field(default=None, max_length=REGION_MAX_LENGTH)
    district: str | None = Field(default=None, max_length=REGION_MAX_LENGTH)

    contact_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    phone: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH)
    email: str | None = Field(default=None, max_length=EMAIL_MAX_LENGTH)
    website: str | None = Field(default=None, max_length=WEBSITE_MAX_LENGTH)

    invoice_recipient: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    invoice_email: str | None = Field(default=None, max_length=EMAIL_MAX_LENGTH)

    payment_term_id: int | None = None
    credit_limit: Decimal | None = Field(default=None, ge=0)
