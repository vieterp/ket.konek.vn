"""Định mức nguyên vật liệu của một mã hàng (FR-SYS-044, lát 8C-2).

Một dòng = một linh kiện cần cho **một đơn vị chính** của thành phẩm, kèm tỷ lệ
phân bổ giá trị dùng khi tháo dỡ (SRS 09 §2.3: "nhập các linh kiện theo tỷ lệ
phân bổ giá trị"). Lắp ráp không cần tỷ lệ — giá thành phẩm là tổng giá trị
linh kiện xuất, engine tính.

**Một cấp, không phải cây.** Thành phẩm B lắp từ A, A lắp từ linh kiện: hai bộ
định mức riêng trên hai mã hàng, và lắp B là **hai phiếu** (lắp A rồi lắp B) —
đúng chữ "lắp ráp nhiều vòng" của FR-STK-005, và cùng lý do `item_units` phẳng:
mỗi cấp nổ định mức là một phép nhân nữa với số lượng lẻ. Dịch vụ chỉ chặn
**vòng** (A cần B, B cần A) vì một vòng ở danh mục là hai phiếu lắp ráp chéo
không hội tụ được ở engine.

**Không** có `unit_id`: số lượng theo đơn vị **chính** của linh kiện — dòng
phiếu lắp ráp sinh từ đây mang `unit_id = base_unit_id` của linh kiện; người
lập phiếu đổi đơn vị trên phiếu nếu muốn (số lượng quy về đơn vị chính bằng
`factor`, như mọi dòng phiếu kho).

Chứng từ **chép** con số của định mức lúc lập (số lượng linh kiện, tỷ lệ) và
được lệch: định mức là gợi ý, báo cáo "so NVL thực xuất với định mức" (SRS 09
§5 #9) cần thấy lệch. Vì thế dòng ở đây không phải thứ chứng từ cũ trỏ tới, và
xóa là đường đúng — cùng lập luận không có `is_active` ở `item_units`.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

ITEM_BOM_LINE_TABLE_NAME = "item_bom_lines"

ALLOCATION_RATIO_PRECISION = 18
ALLOCATION_RATIO_SCALE = 6
"""Cùng hình dạng với `inventory_voucher_lines.allocation_ratio` — dòng phiếu
tháo dỡ chép cột này nguyên văn."""


class ItemBomLine(DatasetBase, Audited, RowVersioned):
    """Một linh kiện trong định mức của một mã hàng thành phẩm."""

    __tablename__ = ITEM_BOM_LINE_TABLE_NAME
    __table_args__ = (
        # Một linh kiện khai hai lần cho cùng thành phẩm là hai số lượng cho cùng
        # một phép nổ định mức, và không câu nào chọn được cái đúng.
        UniqueConstraint("item_id", "component_item_id", name="uq_item_bom_lines_item_component"),
        # Thành phẩm lắp từ chính nó là vòng ngắn nhất — DB chặn được vòng này,
        # vòng dài hơn (A→B→A) do dịch vụ chặn bằng recursive CTE.
        CheckConstraint("item_id <> component_item_id", name="component_differs"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("allocation_ratio > 0", name="allocation_ratio_positive"),
        Index(f"ix_{ITEM_BOM_LINE_TABLE_NAME}_item_id", "item_id"),
        Index(f"ix_{ITEM_BOM_LINE_TABLE_NAME}_component_item_id", "component_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """Thành phẩm. `CASCADE`: định mức **thuộc về** mã hàng thành phẩm — cùng lập
    luận `item_units`."""

    component_item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=False
    )
    """Linh kiện. `RESTRICT`: xóa một mã hàng đang là linh kiện của định mức khác
    là làm định mức ấy thiếu dòng mà không ai thấy."""

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Số đơn vị **chính** của linh kiện cho **một** đơn vị chính thành phẩm."""

    allocation_ratio: Mapped[Decimal] = mapped_column(
        Numeric(ALLOCATION_RATIO_PRECISION, ALLOCATION_RATIO_SCALE),
        nullable=False,
        default=Decimal(1),
        server_default="1",
    )
    """Tỷ lệ phân bổ giá trị thành phẩm về linh kiện này khi tháo dỡ — tương
    đối (`r_i / Σr`), mặc định 1 = chia đều theo dòng."""
