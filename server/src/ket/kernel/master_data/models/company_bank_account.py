"""Tài khoản ngân hàng **của doanh nghiệp** (`docs/srs/04` §2, FR-BNK-002).

Danh mục thứ hai mươi mốt, thêm ở lát 6A vì hai người dùng cùng cần nó và cả
hai đều không thuộc module `bank`:

* mọi chứng từ tiền gửi bắt buộc gắn **tài khoản ngân hàng cụ thể**, không chỉ
  TK kế toán 112 (FR-BNK-002) — `bank_vouchers.bank_account_id` trỏ về đây;
* sheet **ngân hàng (kind 1)** của số dư đầu kỳ (`posting.opening_balances`,
  hoãn từ 4C) nhập chi tiết theo từng tài khoản — mà `posting` không được
  import `modules.bank` (luật phụ thuộc C4), nên danh mục phải nằm ở kernel,
  cùng chỗ với `banks` mà nó trỏ vào.

Khác `partner_bank_accounts` (bảng con của đối tác, lát 3B-2): đây là danh mục
đứng độc lập với mã/tên/ngừng-theo-dõi đầy đủ — nó xuất hiện ở hàng thẻ màn
"Tiền vào tiền ra" và ở bộ lọc mọi báo cáo tiền gửi, tức là một `MasterDataRow`
thật sự chứ không phải chi tiết của bản ghi khác.

`code` chính là **số tài khoản**: đó là cái mã mà kế toán đọc, tra và in — một
cột `account_number` riêng sẽ chỉ là `code` chép lại dưới tên khác.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import (
    NAME_MAX_LENGTH,
    MasterDataRow,
    master_data_table_args,
)
from ket.kernel.master_data.models.partner_bank_account import BANK_BRANCH_MAX_LENGTH

COMPANY_BANK_ACCOUNT_TABLE_NAME = "company_bank_accounts"

CURRENCY_CODE_MAX_LENGTH = 3
"""ISO 4217 — cùng độ dài với `currencies.code` (kernel/currency)."""


def _company_bank_account_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(COMPANY_BANK_ACCOUNT_TABLE_NAME),
        Index(f"ix_{COMPANY_BANK_ACCOUNT_TABLE_NAME}_bank_id", "bank_id"),
    )


class CompanyBankAccount(MasterDataRow):
    """Một tài khoản ngân hàng doanh nghiệp mở tại một ngân hàng."""

    __tablename__ = COMPANY_BANK_ACCOUNT_TABLE_NAME
    __table_args__ = _company_bank_account_table_args()

    bank_id: Mapped[int] = mapped_column(
        ForeignKey("banks.id", ondelete="RESTRICT"), nullable=False
    )
    """Ngân hàng **bắt buộc** — cùng lý do với `partner_bank_accounts.bank_id`:
    số tài khoản không kèm ngân hàng thì không lên được ủy nhiệm chi."""

    currency_code: Mapped[str | None] = mapped_column(
        String(CURRENCY_CODE_MAX_LENGTH),
        ForeignKey("currencies.code", ondelete="RESTRICT"),
        nullable=True,
    )
    """Tiền tệ của tài khoản — quyết định nó hạch toán vào 1121 hay 1122, và
    sheet số dư đầu kỳ nhập nguyên tệ nào. `NULL` = **đồng hạch toán** (VND):
    danh mục tiền tệ là dữ liệu người dùng tự khai (lát 3A), một dữ liệu kế
    toán mới chưa khai đồng nào vẫn phải mở được tài khoản ngân hàng VND —
    cùng lối `exchange_rate_service` không bắt khai tỷ giá VND→VND."""

    account_holder: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    """Tên chủ tài khoản in trên chứng từ — thường là tên doanh nghiệp, nhưng
    chi nhánh hạch toán phụ thuộc có thể mở tài khoản mang tên chi nhánh."""

    bank_branch: Mapped[str | None] = mapped_column(String(BANK_BRANCH_MAX_LENGTH), nullable=True)
    """Chi nhánh ngân hàng mở tài khoản — chữ trên giấy, không phải chi nhánh
    doanh nghiệp (chi nhánh doanh nghiệp là `branch_id` của `MasterDataRow`)."""


class CompanyBankAccountFields(BaseModel):
    """Phần riêng của tài khoản ngân hàng doanh nghiệp trên API (`CatalogSpec`)."""

    bank_id: int = Field(title="Ngân hàng")
    currency_code: str = Field(title="Tiền tệ", default="VND", max_length=CURRENCY_CODE_MAX_LENGTH)
    account_holder: str | None = Field(
        title="Chủ tài khoản", default=None, max_length=NAME_MAX_LENGTH
    )
    bank_branch: str | None = Field(
        title="Chi nhánh ngân hàng", default=None, max_length=BANK_BRANCH_MAX_LENGTH
    )

    @field_validator("account_holder", "bank_branch", "currency_code", mode="before")
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """Chuỗi rỗng vào thành `None` — cùng lý do với `BankFields`."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("currency_code")
    @classmethod
    def _currency_upper(cls, value: str | None) -> str | None:
        """Mã tiền tệ chuẩn hóa chữ hoa: `vnd` và `VND` là một, và FK tới
        `currencies.code` chỉ biết một cách viết."""
        return value.strip().upper() if value is not None else None
