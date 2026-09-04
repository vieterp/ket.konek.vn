"""Dòng của một bảng giá — đơn giá theo mã hàng, đơn vị và ngưỡng số lượng.

Nửa còn lại của FR-SAL-020: đầu bảng giá (`price_list.py`) nói **áp cho ai và
khi nào**, dòng ở đây nói **hàng gì, bao nhiêu**. Trục "số lượng" của FR-SAL-020
nằm ở `min_quantity`.

**`min_quantity` là `NOT NULL` mặc định 1, không phải nullable.** "Bảng giá không
phân theo số lượng" chỉ là một bảng giá mà mọi dòng có ngưỡng 1 — cùng một dữ
liệu, một cách viết. Cho phép `NULL` là mở ra hai cách viết cho cùng một ý và bắt
mọi câu truy vấn mang theo một `COALESCE`.

**Ngưỡng, không phải khoảng** — cùng lập luận đã ghi ở `item_discount_tier.py`:
dòng áp là dòng có `min_quantity` lớn nhất mà vẫn `≤` số lượng, nên khoảng là hệ
quả của thứ tự chứ không phải dữ liệu ai đó phải giữ cho khớp.

**`unit_id NULL` = theo đơn vị chính**, cùng quy ước và cùng lý do với
`item_price_levels` — kể cả ca mã dịch vụ không có đơn vị chính nào. Hai bảng giá
đọc giống nhau thì bộ định giá không phải mang hai luật.

Bảng này khác `item_price_levels` ở đúng một điều: giá ở đây thuộc về **một bảng
giá có phạm vi**, còn giá ở kia thuộc về **chính mã hàng**. Đó là hai tầng khác
nhau của thứ tự nguồn giá, không phải hai bản sao — gộp chúng lại sẽ buộc mọi mức
giá của danh mục phải thuộc về một bảng giá giả "mặc định", và bảng giá giả ấy là
thứ người dùng xóa nhầm được.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.master_data.models.price_list import PRICE_LIST_TABLE_NAME
from ket.kernel.money import UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

PRICE_LIST_LINE_TABLE_NAME = "price_list_lines"


class PriceListLine(DatasetBase, Audited, RowVersioned):
    """Một đơn giá trong một bảng giá, cho một mã hàng từ một ngưỡng số lượng."""

    __tablename__ = PRICE_LIST_LINE_TABLE_NAME
    __table_args__ = (
        # Tách đôi theo `unit_id IS NULL` vì `UNIQUE` thường coi mọi `NULL` là
        # khác nhau — cùng cách vá và cùng lý do với `item_price_levels`.
        Index(
            f"uq_{PRICE_LIST_LINE_TABLE_NAME}_base_unit",
            "price_list_id",
            "item_id",
            "min_quantity",
            unique=True,
            postgresql_where=text("unit_id IS NULL"),
        ),
        Index(
            f"uq_{PRICE_LIST_LINE_TABLE_NAME}_alt_unit",
            "price_list_id",
            "item_id",
            "unit_id",
            "min_quantity",
            unique=True,
            postgresql_where=text("unit_id IS NOT NULL"),
        ),
        CheckConstraint("min_quantity > 0", name="min_quantity_is_positive"),
        CheckConstraint("price >= 0", name="price_is_not_negative"),
        # Câu bộ định giá hỏi: "mọi dòng của bảng giá này cho mã hàng này".
        Index(
            f"ix_{PRICE_LIST_LINE_TABLE_NAME}_list_item",
            "price_list_id",
            "item_id",
        ),
        Index(f"ix_{PRICE_LIST_LINE_TABLE_NAME}_item_id", "item_id"),
        Index(f"ix_{PRICE_LIST_LINE_TABLE_NAME}_unit_id", "unit_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    price_list_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PRICE_LIST_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: dòng **thuộc về** bảng giá, không tồn tại độc lập."""

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=False
    )
    """`RESTRICT` chứ không `CASCADE`, ngược với `item_price_levels.item_id`.

    Ở kia dòng giá là **một phần hồ sơ mã hàng** nên nó biến mất cùng mã hàng; ở
    đây mã hàng chỉ được **nhắc tới** trong một bảng giá thuộc về ai khác, và xóa
    lặng lẽ một dòng khỏi bảng giá của khách hàng là đổi giá bán của họ mà không
    ai thấy."""

    unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )
    """Đơn vị mà giá này tính theo. `NULL` = đơn vị chính của mã hàng."""

    min_quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE),
        nullable=False,
        default=Decimal(1),
        server_default="1",
    )
    """Số lượng tối thiểu để dòng này áp, tính theo **`unit_id` của chính dòng**.

    Theo đơn vị của dòng chứ không đơn vị chính, ngược với
    `item_discount_tiers.min_quantity` — và bất đối xứng ấy có chủ đích: bậc chiết
    khấu là một thang **của mã hàng**, phải so được giữa các dòng chứng từ ghi
    bằng đơn vị khác nhau, nên nó cần một đơn vị chuẩn. Còn dòng bảng giá đã tự
    mang đơn vị của nó, nên "từ 10 thùng trở lên giá này" đọc đúng như người
    dùng khai và không phải quy đổi gì."""

    price: Mapped[Decimal] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=False
    )
    """Đơn giá VND — cùng phạm vi tiền tệ với `item_price_levels.price`."""
