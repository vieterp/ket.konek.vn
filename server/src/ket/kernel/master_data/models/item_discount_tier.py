"""Bậc chiết khấu theo số lượng của một mã hàng (FR-SYS-045).

"Bậc chiết khấu theo số lượng, **tự áp dụng** khi lập chứng từ bán" — hai chữ
"tự áp dụng" là điều phân biệt bảng này với `item_price_levels`. Mức giá thì
người lập chứng từ chọn; bậc chiết khấu thì không ai chọn, số lượng trên dòng
quyết định. Vì thế bảng này không có `level` do người dùng đặt, mà có
`min_quantity` — ngưỡng số lượng để bậc có hiệu lực.

**Luật chọn bậc:** bậc áp là bậc có `min_quantity` **lớn nhất** mà vẫn `≤` số
lượng trên dòng. Không bậc nào thỏa (mua ít hơn ngưỡng thấp nhất) thì chiết khấu
**0**, không phải lỗi — bảng bậc là ưu đãi, không phải điều kiện bán hàng.

**Không có `max_quantity`.** Một bảng khoảng-đóng (`từ..đến`) diễn đạt được cùng
thứ nhưng thêm được hai trạng thái sai mà bảng ngưỡng không có: khoảng chồng
nhau (hai bậc cùng thỏa) và khoảng hở (số lượng rơi vào chỗ không bậc nào phủ,
mà người khai tưởng là có). Với bảng ngưỡng, "khoảng" là hệ quả của thứ tự chứ
không phải dữ liệu ai đó phải giữ cho khớp.

**`discount_percent` chứ không `discount_amount`.** FR-SYS-045 nói "bậc chiết
khấu", và một số tiền tuyệt đối khai ở danh mục thì vô nghĩa khi mã hàng bán ở
nhiều mức giá — cùng một 50.000đ là 5% ở giá bán buôn và 20% ở giá đại lý. Tiền
chiết khấu là kết quả tính trên dòng chứng từ (`discount_amount` của
`sales_invoice_lines`), không phải dữ liệu danh mục.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

ITEM_DISCOUNT_TIER_TABLE_NAME = "item_discount_tiers"

DISCOUNT_PERCENT_PRECISION = 5
DISCOUNT_PERCENT_SCALE = 2
"""`NUMERIC(5,2)` — tối đa 999.99, cùng hình dạng với thuế suất GTGT trên dòng
chứng từ. Trần thật (100) do `CHECK` canh, không do độ rộng cột."""


class ItemDiscountTier(DatasetBase, Audited, RowVersioned):
    """Một bậc: mua từ `min_quantity` trở lên thì được `discount_percent`."""

    __tablename__ = ITEM_DISCOUNT_TIER_TABLE_NAME
    __table_args__ = (
        # Hai bậc cùng ngưỡng là hai tỷ lệ cho cùng một số lượng, và luật "lấy
        # ngưỡng lớn nhất ≤ số lượng" không phân giải được cặp ấy.
        UniqueConstraint(
            "item_id", "min_quantity", name="uq_item_discount_tiers_item_min_quantity"
        ),
        # Ngưỡng 0 làm bậc ấy phủ **mọi** dòng kể cả dòng số lượng 0, tức nó
        # không còn là một bậc mà là chiết khấu mặc định — thứ FR-SYS-045 không
        # nói tới và bảng giá mới là chỗ diễn đạt.
        CheckConstraint("min_quantity > 0", name="min_quantity_is_positive"),
        # Trên 100% là bán hàng rồi trả thêm tiền cho khách. Đúng 100% thì hợp lệ:
        # hàng khuyến mại có kèm điều kiện đi qua đúng hình dạng ấy.
        CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name="discount_percent_within_bounds",
        ),
        Index(f"ix_{ITEM_DISCOUNT_TIER_TABLE_NAME}_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: bậc chiết khấu **thuộc về** mã hàng — cùng lập luận `item_unit.py`."""

    min_quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Ngưỡng số lượng, tính theo **đơn vị chính** của mã hàng.

    Đơn vị chính chứ không đơn vị trên dòng chứng từ, và bộ chọn bậc quy đổi số
    lượng của dòng về đơn vị chính trước khi so. Nếu ngưỡng tính theo đơn vị của
    dòng thì cùng một bảng bậc cho ra ưu đãi khác nhau tùy người nhập gõ "2
    thùng" hay "48 chiếc" — hai cách viết của cùng một lượt mua."""

    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(DISCOUNT_PERCENT_PRECISION, DISCOUNT_PERCENT_SCALE), nullable=False
    )
    """Tỷ lệ chiết khấu thương mại, tính trên thành tiền trước thuế của dòng."""
