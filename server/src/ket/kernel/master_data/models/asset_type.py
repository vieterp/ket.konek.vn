"""Loại tài sản cố định (`docs/srs/01` §7, FR-SYS-052).

Giá trị ngầm định chảy xuống bản ghi TSCĐ lúc khai báo (phase 8): chọn loại
"Máy móc thiết bị" thì thời gian khấu hao điền sẵn thay vì kế toán viên tra
khung Thông tư 45 cho từng tài sản một.

`default_useful_life_months` là **gợi ý**, không phải ràng buộc: khung khấu hao
của TT45 là một dải (ví dụ máy móc 3–15 năm), và tài sản cụ thể được đặt khác
đi. Vì thế nó `NULL` được, và phase 8 chép giá trị xuống bản ghi tài sản chứ
không đọc ngược lên loại — đổi mặc định của loại **không** được làm đổi số khấu
hao đã tính của những tài sản đã khai.

Tài khoản chi phí khấu hao ngầm định mà FR-SYS-052 nêu chưa khai ở đây: đích của
nó là `chart_of_accounts`, thuộc gói cấu hình phase 5 (H50).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

ASSET_TYPE_TABLE_NAME = "asset_types"


def _asset_type_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(ASSET_TYPE_TABLE_NAME),
        # `0` tháng nghĩa là chia cho không ở mọi công thức khấu hao. Chặn ở DB
        # thay vì ở form: giá trị này cũng đi vào qua nhập Excel (lát 3C).
        CheckConstraint(
            "default_useful_life_months IS NULL OR default_useful_life_months > 0",
            name="default_useful_life_months_positive",
        ),
    )


class AssetType(MasterDataRow):
    """Một nhóm tài sản cố định mang thời gian khấu hao ngầm định."""

    __tablename__ = ASSET_TYPE_TABLE_NAME
    __table_args__ = _asset_type_table_args()

    default_useful_life_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Thời gian khấu hao ngầm định, tính bằng tháng. `NULL` = không gợi ý."""


class AssetTypeFields(BaseModel):
    """Phần riêng của loại TSCĐ trên API (`registry.CatalogSpec`)."""

    default_useful_life_months: int | None = Field(
        title="Thời gian sử dụng ngầm định (tháng)", default=None, gt=0
    )
