"""Hình dạng request/response của hai bảng con vật tư hàng hóa (FR-SYS-041/046)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ket.kernel.master_data.base import CODE_MAX_LENGTH, NAME_MAX_LENGTH
from ket.kernel.master_data.models.item_discount_tier import (
    DISCOUNT_PERCENT_PRECISION,
    DISCOUNT_PERCENT_SCALE,
)
from ket.kernel.master_data.models.item_price_level import (
    PRICE_LABEL_MAX_LENGTH,
    PRICE_LEVEL_MAX,
    PRICE_LEVEL_MIN,
    PriceDirection,
)
from ket.kernel.money import UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

FactorField = Field(gt=0, decimal_places=None)
"""Tỷ lệ quy đổi phải **dương**: 0 biến mọi số lượng thành 0, số âm biến nhập
thành xuất. Trùng với `CHECK factor > 0` của DB có chủ đích — DB là chỗ bảo đảm
(mọi đường ghi đi qua nó, kể cả nhập liệu), còn ở đây là chỗ trả về câu tiếng
Việt nêu ô phải sửa thay vì tên một ràng buộc nội bộ.

`decimal_places=None` để Pydantic **không** tự làm tròn: số chữ số thập phân lưu
trữ do cột quyết định (`kernel/quantity.py`), và một tầng làm tròn thứ hai đặt ở
biên API là chỗ con số đổi giá trị mà không ai thấy."""

CodeField = Field(min_length=1, max_length=CODE_MAX_LENGTH)
NameField = Field(min_length=1, max_length=NAME_MAX_LENGTH)


class _TrimmedText(BaseModel):
    """Cắt khoảng trắng hai đầu của mã và tên quy cách trước mọi phép kiểm.

    `min_length=1` và `CHECK code <> ''` đều **để lọt** một chuỗi chỉ gồm khoảng
    trắng, và `uq_item_variants_item_code` coi `"DO"` với `"DO "` là hai quy cách
    khác nhau (review M-4). Với một nhãn hiển thị thì đó là chuyện nhỏ; với mã quy
    cách thì không — nó là **trục khóa** của báo cáo tồn kho phase 8, đúng lý do
    H65 không hoãn bảng này, nên hai mã lệch nhau một dấu cách là hai cột tồn kho
    cho cùng một thứ.

    Cắt (`strip`) chứ không từ chối: người dùng dán mã từ Excel thì khoảng trắng
    thừa là chuyện thường, và từ chối một thứ hệ thống tự sửa được là bắt họ làm
    việc của máy. Chuỗi rỗng sau khi cắt thì `min_length=1` từ chối.

    Phạm vi có ý thức: `CodeField`/`NameField` **chung** của hai mươi danh mục
    (`master_data_schemas.py`) cũng có yếu điểm này — nợ cũ, đổi ở đó là đổi hành
    vi của mọi danh mục đã giao ở 3B-1/3B-2 nên nó cần một quyết định riêng.
    """

    @field_validator("code", "name", mode="before", check_fields=False)
    @classmethod
    def _trim(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ItemUnitResponse(BaseModel):
    """Một đơn vị quy đổi của mã hàng."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    unit_id: int
    factor: Decimal
    row_version: int


class ItemUnitListResponse(BaseModel):
    """Toàn bộ đơn vị quy đổi của một mã hàng — không phân trang, xem router."""

    items: list[ItemUnitResponse]


class ItemUnitCreateRequest(BaseModel):
    """Thêm một đơn vị quy đổi.

    `unit_id` phải **khác** đơn vị chính của mã hàng: đơn vị chính luôn có tỷ lệ 1
    và không nằm trong bảng này (`ItemUnitService._ensure_convertible`).
    """

    model_config = ConfigDict(extra="forbid")

    unit_id: int = Field(ge=1)
    factor: Decimal = FactorField


class ItemUnitUpdateRequest(BaseModel):
    """Sửa một đơn vị quy đổi — gửi **trọn** giá trị mới, có kiểm phiên bản."""

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    unit_id: int = Field(ge=1)
    factor: Decimal = FactorField


class ItemVariantResponse(BaseModel):
    """Một mã quy cách của mã hàng."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    code: str
    name: str
    is_active: bool
    row_version: int


class ItemVariantListResponse(BaseModel):
    """Toàn bộ quy cách của một mã hàng — không phân trang, xem router."""

    items: list[ItemVariantResponse]


class ItemVariantCreateRequest(_TrimmedText):
    """Thêm một mã quy cách."""

    model_config = ConfigDict(extra="forbid")

    code: str = CodeField
    name: str = NameField


class ItemVariantUpdateRequest(_TrimmedText):
    """Sửa một mã quy cách, gồm cả cờ còn dùng — gửi **trọn** giá trị mới."""

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    code: str = CodeField
    name: str = NameField
    is_active: bool


PriceField = Field(ge=0, max_digits=UNIT_PRICE_PRECISION, decimal_places=UNIT_PRICE_SCALE)
"""Đơn giá: không âm (giá 0 hợp lệ — hàng khuyến mại), đúng độ rộng cột."""

MinQuantityField = Field(gt=0, max_digits=QUANTITY_PRECISION, decimal_places=QUANTITY_SCALE)
"""Ngưỡng số lượng: dương hẳn — ngưỡng 0 phủ mọi dòng nên nó không còn là một bậc."""


class ItemPriceLevelResponse(BaseModel):
    """Một mức giá của mã hàng."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    unit_id: int | None
    direction: PriceDirection
    level: int
    price: Decimal
    label: str | None
    row_version: int


class ItemPriceLevelListResponse(BaseModel):
    """Toàn bộ mức giá của một mã hàng — không phân trang, xem router."""

    items: list[ItemPriceLevelResponse]


class ItemPriceLevelCreateRequest(BaseModel):
    """Thêm một mức giá.

    `unit_id` bỏ trống = giá theo **đơn vị chính**. Gửi id của đơn vị chính lên
    thì bị từ chối: đó là cách viết thứ hai cho cùng một dòng
    (`unit_priced_rows.ensure_unit_is_priceable`).
    """

    model_config = ConfigDict(extra="forbid")

    unit_id: int | None = Field(default=None, ge=1)
    direction: PriceDirection
    level: int = Field(ge=PRICE_LEVEL_MIN, le=PRICE_LEVEL_MAX)
    price: Decimal = PriceField
    label: str | None = Field(default=None, max_length=PRICE_LABEL_MAX_LENGTH)


class ItemPriceLevelUpdateRequest(ItemPriceLevelCreateRequest):
    """Sửa một mức giá — gửi **trọn** giá trị mới, có kiểm phiên bản.

    Kế thừa thân request tạo mới chứ không khai lại từng trường, cùng lối
    `ItemFields` kế thừa `ItemEditableFields`: mọi luật miền giá trị vì thế áp cho
    cả hai đường, không có luật nào chỉ có ở một bên.
    """

    row_version: int = Field(ge=1)


class ItemDiscountTierResponse(BaseModel):
    """Một bậc chiết khấu theo số lượng của mã hàng."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    min_quantity: Decimal
    discount_percent: Decimal
    row_version: int


class ItemDiscountTierListResponse(BaseModel):
    """Toàn bộ bậc chiết khấu của một mã hàng — không phân trang, xem router."""

    items: list[ItemDiscountTierResponse]


class ItemDiscountTierCreateRequest(BaseModel):
    """Thêm một bậc chiết khấu. Ngưỡng tính theo **đơn vị chính** của mã hàng."""

    model_config = ConfigDict(extra="forbid")

    min_quantity: Decimal = MinQuantityField
    discount_percent: Decimal = Field(
        ge=0,
        le=100,
        max_digits=DISCOUNT_PERCENT_PRECISION,
        decimal_places=DISCOUNT_PERCENT_SCALE,
    )


class ItemDiscountTierUpdateRequest(ItemDiscountTierCreateRequest):
    """Sửa một bậc — gửi **trọn** giá trị mới, có kiểm phiên bản."""

    row_version: int = Field(ge=1)
