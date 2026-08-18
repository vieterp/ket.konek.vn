"""Loại công cụ dụng cụ (`docs/srs/01` §7, FR-SYS-052).

Song song với `asset_type.py` nhưng cho CCDC, và khác ở một chữ có hệ quả kế
toán: TSCĐ **khấu hao**, CCDC **phân bổ**. Cùng hình dạng dữ liệu, hai phân hệ
khác nhau ở phase 8, hai dòng chi phí khác nhau trên báo cáo — nên hai danh mục
chứ không một danh mục có cột "loại tài sản".

`default_allocation_months` là gợi ý chảy xuống bản ghi CCDC lúc khai báo, cùng
lối đã ghi ở `asset_type.py`: chép xuống, không đọc ngược lên.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

TOOL_TYPE_TABLE_NAME = "tool_types"


def _tool_type_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(TOOL_TYPE_TABLE_NAME),
        CheckConstraint(
            "default_allocation_months IS NULL OR default_allocation_months > 0",
            name="default_allocation_months_positive",
        ),
    )


class ToolType(MasterDataRow):
    """Một nhóm công cụ dụng cụ mang kỳ phân bổ ngầm định."""

    __tablename__ = TOOL_TYPE_TABLE_NAME
    __table_args__ = _tool_type_table_args()

    default_allocation_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Số kỳ phân bổ ngầm định, tính bằng tháng. `NULL` = không gợi ý."""


class ToolTypeFields(BaseModel):
    """Phần riêng của loại CCDC trên API (`registry.CatalogSpec`)."""

    default_allocation_months: int | None = Field(
        title="Thời gian phân bổ ngầm định (tháng)", default=None, gt=0
    )
