"""Ngân hàng (`docs/srs/01` §7, FR-SYS-033).

Danh sách ngân hàng. Tài khoản ngân hàng **của doanh nghiệp** (phase 6) và **của
đối tác** (lát 3B-2) đều trỏ về đây, nên nó phải có trước cả hai.

Hai cột riêng, cả hai đều in ra giấy: `short_name` là cái tên xuất hiện trên ủy
nhiệm chi ("Vietcombank" chứ không "Ngân hàng TMCP Ngoại thương Việt Nam"), và
`swift_code` là thứ bắt buộc cho chuyển tiền quốc tế. Cả hai `NULL` được — chi
nhánh ngân hàng trong nước nhỏ thường không có mã SWIFT riêng.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

BANK_TABLE_NAME = "banks"

SHORT_NAME_MAX_LENGTH = 50
SWIFT_CODE_LENGTH_MIN = 8
SWIFT_CODE_LENGTH_MAX = 11
"""ISO 9362: mã SWIFT dài 8 (trụ sở) hoặc 11 ký tự (có mã chi nhánh)."""


def _bank_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(BANK_TABLE_NAME),
        # Chuỗi rỗng và `NULL` cùng nghĩa "chưa khai", và hai cách nói cùng một
        # điều là hai nhánh `if` mà mọi nơi đọc phải nhớ viết. Chặn chuỗi rỗng
        # ngay ở DB để chỉ còn một cách.
        CheckConstraint("short_name <> ''", name="short_name_not_blank"),
        CheckConstraint(
            f"swift_code IS NULL OR length(swift_code) IN "
            f"({SWIFT_CODE_LENGTH_MIN}, {SWIFT_CODE_LENGTH_MAX})",
            name="swift_code_length_iso9362",
        ),
    )


class Bank(MasterDataRow):
    """Một ngân hàng hoặc chi nhánh ngân hàng."""

    __tablename__ = BANK_TABLE_NAME
    __table_args__ = _bank_table_args()

    short_name: Mapped[str | None] = mapped_column(String(SHORT_NAME_MAX_LENGTH), nullable=True)
    """Tên gọi ngắn dùng khi in chứng từ."""

    swift_code: Mapped[str | None] = mapped_column(String(SWIFT_CODE_LENGTH_MAX), nullable=True)
    """Mã SWIFT/BIC (ISO 9362) cho chuyển tiền quốc tế."""


class BankFields(BaseModel):
    """Phần riêng của ngân hàng trên API (`registry.CatalogSpec`)."""

    short_name: str | None = Field(
        title="Tên viết tắt", default=None, max_length=SHORT_NAME_MAX_LENGTH
    )
    swift_code: str | None = Field(title="Mã SWIFT", default=None)

    @field_validator("short_name", "swift_code", mode="before")
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """Chuỗi rỗng (và chuỗi chỉ có khoảng trắng) vào thành `None`.

        Form của trình duyệt gửi `""` cho ô để trống, còn `CHECK` phía DB từ
        chối chuỗi rỗng. Không chuẩn hóa ở đây thì bỏ trống một ô không bắt buộc
        sẽ trả về lỗi ràng buộc — thông điệp khó hiểu nhất có thể cho thao tác
        dễ hiểu nhất.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("swift_code")
    @classmethod
    def _iso9362_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) not in (SWIFT_CODE_LENGTH_MIN, SWIFT_CODE_LENGTH_MAX):
            raise ValueError(
                f"Mã SWIFT phải dài {SWIFT_CODE_LENGTH_MIN} hoặc "
                f"{SWIFT_CODE_LENGTH_MAX} ký tự (ISO 9362)"
            )
        return value
