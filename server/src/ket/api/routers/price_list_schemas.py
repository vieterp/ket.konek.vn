"""Thân request/response của bảng con `price_list_lines` và của đường định giá.

Tách khỏi `master_data_schemas.py` cùng lý do `items_schemas.py` tách: bảng con và
đường định giá không đi qua bộ sinh route của danh mục.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ket.kernel.master_data.models.item_price_level import PriceDirection
from ket.kernel.money import UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE
from ket.kernel.pricing import DEFAULT_PRICE_LEVEL, PriceSource
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

_PriceField = Field(ge=0, max_digits=UNIT_PRICE_PRECISION, decimal_places=UNIT_PRICE_SCALE)


class PriceListLineResponse(BaseModel):
    """Một dòng của bảng giá."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    price_list_id: int
    item_id: int
    unit_id: int | None
    min_quantity: Decimal
    price: Decimal
    row_version: int


class PriceListLineListResponse(BaseModel):
    """Toàn bộ dòng của một bảng giá — không phân trang, xem router."""

    items: list[PriceListLineResponse]


class PriceListLineCreateRequest(BaseModel):
    """Thêm một dòng giá.

    `unit_id` bỏ trống = giá theo **đơn vị chính** của mã hàng; gửi id của đơn vị
    chính lên thì bị từ chối (`unit_priced_rows.ensure_unit_is_priceable`).

    `min_quantity` mặc định 1 = "áp cho mọi số lượng". Bảng giá không phân theo số
    lượng chỉ là bảng giá mà mọi dòng để nguyên mặc định ấy.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)
    unit_id: int | None = Field(default=None, ge=1)
    min_quantity: Decimal = Field(
        default=Decimal(1),
        gt=0,
        max_digits=QUANTITY_PRECISION,
        decimal_places=QUANTITY_SCALE,
    )
    price: Decimal = _PriceField


class PriceListLineUpdateRequest(PriceListLineCreateRequest):
    """Sửa một dòng giá — gửi **trọn** giá trị mới, có kiểm phiên bản."""

    row_version: int = Field(ge=1)


class PriceQuoteRequest(BaseModel):
    """Hỏi đơn giá và chiết khấu cho **một** dòng chứng từ sắp lập.

    Client chỉ hiển thị kết quả — mọi luật chọn giá chạy ở server (plan §Chính
    sách giá & chiết khấu). Endpoint này là cách form mua/bán lấy con số ấy trước
    khi có chứng từ nào được cất.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)
    unit_id: int | None = Field(default=None, ge=1)
    quantity: Decimal = Field(gt=0, max_digits=QUANTITY_PRECISION, decimal_places=QUANTITY_SCALE)
    direction: PriceDirection
    on_date: date
    partner_id: int | None = Field(default=None, ge=1)
    contract_id: int | None = Field(default=None, ge=1)
    price_list_id: int | None = Field(default=None, ge=1)
    """Ép dùng đúng bảng giá này thay vì đi tìm — xem `kernel.pricing.quote_price`."""
    level: int = Field(default=DEFAULT_PRICE_LEVEL, ge=1)
    tax_rate: Decimal = Field(default=Decimal(0), ge=0, le=100)
    """Thuế suất GTGT của dòng, **phần trăm** — chỉ dùng khi giá khai là giá sau thuế."""


class PriceQuoteResponse(BaseModel):
    """Kết quả định giá, kèm **nguồn** đã trả lời.

    Có `source` vì người dùng thấy một đơn giá tự điền và câu hỏi đầu tiên của họ
    là "số này ở đâu ra" — xem `kernel.pricing.PriceSource`.
    """

    model_config = ConfigDict(from_attributes=True)

    unit_price: Decimal
    quoted_price: Decimal
    is_tax_inclusive: bool
    source: PriceSource
    price_list_id: int | None
    level: int | None
    discount_percent: Decimal


BATCH_QUOTE_MAX_LINES = 200
"""Trần số dòng của một lượt hỏi giá theo lô.

Trần này là một quyết định về **tài nguyên**, không phải về hình dạng dữ liệu:
bộ định giá tốn 6–10 truy vấn **mỗi dòng** và lô chạy trong MỘT transaction, nên
trần nhân thẳng vào thời gian một kết nối bị giữ. 200 dòng đã rộng hơn hóa đơn
thật (hóa đơn dài đi kèm bảng kê, FR-SAL-010) mà vẫn chặn trên ~2 000 truy vấn
cho một request.
"""


class PriceQuoteBatchRequest(BaseModel):
    """Hỏi giá cho **cả chứng từ** trong một lượt (FR-SAL §4.2).

    **Nó tiết kiệm cái gì, và không tiết kiệm cái gì** — nói thẳng để không ai
    đọc nhầm: một form 50 dòng hỏi từng dòng là 50 request, mỗi request một
    transaction và một lượt đọc tùy chọn "giá đã gồm thuế" cấp hệ thống. Gộp lô
    đưa 50 request ấy về **một** request, một transaction, một lượt đọc tùy
    chọn. Số truy vấn **mỗi dòng** thì **không đổi**: hàm định giá vẫn chạy
    nguyên vẹn cho từng dòng, nên tổng truy vấn vẫn tuyến tính theo số dòng —
    `BATCH_QUOTE_MAX_LINES` là thứ chặn trên nó.

    Nợ N+1 mà 7C-1 ghi lại (review M-2) nói tới đường **cất chứng từ**; đường ấy
    không còn gọi bộ định giá nữa (quyết định user 2026-09-04: đơn giá do client
    chốt), nên nó không phải là chỗ N+1 chạm tới. Gom lô cho đường **hỏi giá**
    của form là việc còn lại, và phần "làm phẳng truy vấn theo dòng" vẫn đang mở.

    Kết quả trả về **cùng thứ tự và cùng số phần tử** với `lines` — client ghép
    theo vị trí, không theo một khóa nào phải bịa ra.
    """

    model_config = ConfigDict(extra="forbid")

    lines: tuple[PriceQuoteRequest, ...] = Field(min_length=1, max_length=BATCH_QUOTE_MAX_LINES)


class PriceQuoteBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[PriceQuoteResponse, ...]
