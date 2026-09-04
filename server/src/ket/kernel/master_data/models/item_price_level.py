"""Bảng giá nhiều mức của một mã hàng, theo từng đơn vị quy đổi (FR-SYS-042).

FR-SYS-042 nói nguyên văn: "danh sách **đơn giá mua** và **đơn giá bán** nhiều
mức theo từng đơn vị quy đổi". Ba trục trong một câu, nên bảng này có ba cột
trục — `direction` (mua/bán), `unit_id` (đơn vị nào), `level` (mức mấy) — và
khóa duy nhất gộp cả ba cùng `item_id`.

**Vì sao một bảng cho cả hai chiều** thay vì `item_purchase_prices` và
`item_sale_prices`: hai bảng giống hệt nhau tới từng ràng buộc là hai chỗ để
cùng một luật lệch nhau, và mọi câu hỏi mà bộ định giá đặt ("giá mức 2 của mã
này theo thùng") có đúng một hình dạng bất kể chiều. Chiều là **dữ liệu**, không
phải cấu trúc.

**Vì sao không có cột `sale_price` trên `items`.** Thứ tự nguồn giá của phase 7
(§Chính sách giá & chiết khấu) có ba tầng, tầng cuối là "đơn giá mặc định trên
danh mục". Tầng ấy **là** mức 1 ở đơn vị chính — không phải một con số thứ hai
sống cạnh bảng này. Một cột riêng thì mọi màn hình phải chọn đọc cột hay đọc
bảng, và hai lựa chọn ấy sẽ lệch nhau ở đúng những mã hàng ai đó sửa một bên.

**`level` là số thứ tự, không phải bậc số lượng.** Mức giá của FR-SYS-042 là
"giá bán buôn / giá bán lẻ / giá đại lý" — một thang do doanh nghiệp tự đặt tên
ngoài hệ thống, chọn tay lúc lập chứng từ. Bậc theo **số lượng** là việc của
`item_discount_tiers` (FR-SYS-045) và nó tự áp, không ai chọn. Gộp hai khái niệm
vào một cột là cách chắc chắn để một trong hai mất khả năng diễn đạt.

**`unit_id NULL` nghĩa là "theo đơn vị chính"**, không phải "thiếu dữ liệu" — và
đó là cách tầng 3 của thứ tự nguồn giá ("đơn giá mặc định trên danh mục") được
diễn đạt mà không cần một cột thứ hai. Cùng bất đối xứng với `item_units`, nơi
đơn vị chính **không** có dòng vì tỷ lệ của nó luôn là 1: ở đây đơn vị chính
không cần ghi tên vì `items.base_unit_id` đã nói nó là ai, và ghi lại là mở ra
ca hai dòng cùng nghĩa mà chỉ số duy nhất không thấy.

Nó còn gỡ đúng một ca mà cột `NOT NULL` sẽ chặn oan: **dịch vụ không bắt buộc có
đơn vị chính** (`stock_item_needs_base_unit` chỉ ràng hàng hóa và thành phẩm), mà
`base_unit_id` thì chốt một lần lúc tạo (H69) — nên một mã dịch vụ tạo không kèm
đơn vị sẽ **vĩnh viễn** không khai được giá bán nếu giá buộc phải có đơn vị.

`unit_id` khác `NULL` thì phải là một đơn vị quy đổi **đã khai** của chính mã
hàng và **khác** đơn vị chính — hai phép kiểm so cột của bảng này với dòng của
bảng khác, thứ `CHECK` không làm được, nên `ItemPriceLevelService._ensure_
priceable` canh, cùng khuôn `ItemUnitService._ensure_convertible`. Khóa ngoại
trỏ thẳng `units_of_measure` chứ không `item_units` vì điều kiện thật là một
phép hợp, và vì tên đơn vị không nên mất khi ai đó xóa dòng quy đổi.
"""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.money import UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

ITEM_PRICE_LEVEL_TABLE_NAME = "item_price_levels"

PRICE_LABEL_MAX_LENGTH = 100

PRICE_LEVEL_MIN = 1
PRICE_LEVEL_MAX = 20
"""Trần số mức. Không phải giới hạn nghiệp vụ — không doanh nghiệp nào có hai
mươi thang giá bán — mà là chặn trên cho lượt nhập liệu gõ nhầm một số bảy chữ
số vào ô "mức", vì `level` đi vào khóa duy nhất và vào thứ tự hiển thị."""


class PriceDirection(IntEnum):
    """Chiều của một mức giá — `item_price_levels.direction`.

    `IntEnum` chứ không hằng số rời như `PurchaseInvoiceKind`: khác với `kind` của
    chứng từ (mở rộng theo nghiệp vụ mới), chiều giá đóng ở đúng hai giá trị —
    mua và bán là hai đầu của một giao dịch, không có đầu thứ ba — nên bộ giá trị
    đóng của một enum là mô tả đúng, và nó cho bộ định giá một tham số gõ đúng
    kiểu thay vì một `int` trần.
    """

    PURCHASE = 0
    """Đơn giá mua — gợi ý lên dòng chứng từ mua hàng."""
    SALE = 1
    """Đơn giá bán — tầng 2 và tầng 3 của thứ tự nguồn giá."""


class ItemPriceLevel(DatasetBase, Audited, RowVersioned):
    """Một mức giá của một mã hàng, ở một đơn vị, theo một chiều."""

    __tablename__ = ITEM_PRICE_LEVEL_TABLE_NAME
    __table_args__ = (
        # Hai dòng cùng (mã hàng, đơn vị, chiều, mức) là hai giá cho cùng một câu
        # hỏi, và bộ định giá không có căn cứ nào để chọn — cùng lập luận với
        # `uq_item_units_item_unit`. Tách đôi theo `unit_id IS NULL` vì một
        # `UNIQUE` thường coi mọi `NULL` là khác nhau, nên nó cho phép khai vô số
        # dòng "giá theo đơn vị chính" cùng mức — đúng thứ ràng buộc này sinh ra
        # để chặn. Cùng cách vá với `uq_{bảng}_shared_code` ở `master_data/base.py`.
        Index(
            f"uq_{ITEM_PRICE_LEVEL_TABLE_NAME}_base_unit",
            "item_id",
            "direction",
            "level",
            unique=True,
            postgresql_where=text("unit_id IS NULL"),
        ),
        Index(
            f"uq_{ITEM_PRICE_LEVEL_TABLE_NAME}_alt_unit",
            "item_id",
            "unit_id",
            "direction",
            "level",
            unique=True,
            postgresql_where=text("unit_id IS NOT NULL"),
        ),
        # Giá âm biến một lượt bán thành một lượt trả lại ở mọi phép cộng phía
        # sau. Giá **0** thì hợp lệ: hàng khuyến mại không kèm điều kiện bán giá 0
        # là nghiệp vụ có thật (SRS 06 §3.2).
        CheckConstraint("price >= 0", name="price_is_not_negative"),
        CheckConstraint(
            f"level BETWEEN {PRICE_LEVEL_MIN} AND {PRICE_LEVEL_MAX}",
            name="level_within_bounds",
        ),
        CheckConstraint("direction IN (0, 1)", name="direction_is_known"),
        # Chỉ số phủ đúng câu bộ định giá hỏi: "mọi mức của mã này, chiều này".
        Index(
            f"ix_{ITEM_PRICE_LEVEL_TABLE_NAME}_item_direction",
            "item_id",
            "direction",
        ),
        Index(f"ix_{ITEM_PRICE_LEVEL_TABLE_NAME}_unit_id", "unit_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: mức giá **thuộc về** mã hàng, không tồn tại độc lập — cùng lập
    luận đã ghi ở `item_unit.py`."""

    unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )
    """Đơn vị mà giá này tính theo. `NULL` = **đơn vị chính của mã hàng**.

    `RESTRICT` chứ không `CASCADE`, khác `item_id`: xóa một đơn vị tính đang có
    giá treo là làm biến mất con số mà người dùng không hề đụng tới."""

    direction: Mapped[PriceDirection] = mapped_column(SmallInteger, nullable=False)
    """`PriceDirection` lưu dạng số nguyên, cùng lối `vouchers.kind`."""

    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """Thứ tự mức, đếm từ 1. Mức 1 với `unit_id IS NULL` là "đơn giá mặc định
    trên danh mục" mà thứ tự nguồn giá gọi ở tầng cuối."""

    price: Mapped[Decimal] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=False
    )
    """Đơn giá, **nguyên tệ của bảng giá là VND** ở lát này.

    Bảng giá đa tiền tệ không có trong FR-SYS-042 và không có màn hình nào ở
    phase 7 hỏi tới nó; thêm cột `currency_code` bây giờ là thêm một trục mà mọi
    câu truy vấn phải mang theo giá trị mặc định. Nếu phase sau cần, đó là một
    `ALTER TABLE` cộng một điều kiện ở bộ định giá — cùng khuôn H72 đã áp cho
    chính cờ giá sau thuế."""

    label: Mapped[str | None] = mapped_column(String(PRICE_LABEL_MAX_LENGTH), nullable=True)
    """Tên thang giá do doanh nghiệp đặt ("bán buôn", "đại lý") — chỉ để hiển thị.

    Không tham gia phép chọn nào: bộ định giá chọn theo `level`, vì tên là thứ
    người dùng sửa được bất cứ lúc nào và một chứng từ cũ tham chiếu tên đã đổi
    thì không truy lại được mức nào đã áp."""
