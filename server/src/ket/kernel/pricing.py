"""Bộ định giá: đơn giá và chiết khấu của một dòng chứng từ (FR-SAL §4.2, FR-SYS-042/043/045).

Ba tầng nguồn giá của phase 7 (§Chính sách giá & chiết khấu), xét theo đúng thứ
tự này và **dừng ở tầng đầu tiên trả lời được**:

1. **Bảng giá theo đối tác / hợp đồng** (`price_lists` + `price_list_lines`,
   FR-SAL-020) — hẹp nhất, nên xét trước.
2. **Mức giá của mã hàng theo đúng đơn vị** (`item_price_levels`, FR-SYS-042).
3. **Đơn giá mặc định trên danh mục** — vẫn là `item_price_levels`, nhưng dòng
   `unit_id IS NULL` mức 1. Không phải một bảng thứ ba: xem đầu
   `models/item_price_level.py`.

Không tầng nào trả lời thì kết quả là `PriceSource.NONE` kèm đơn giá `0`, **không
phải một lỗi**: mã hàng chưa khai giá là chuyện thường ngày, và người lập chứng từ
gõ tay đơn giá là đường hợp lệ. Ném lỗi ở đây sẽ biến một ô trống thành một cái
chặn.

**Vì sao ở `kernel` chứ không `modules/sales` như phác thảo plan.** Cả hai bảng
giá mang cột `direction` với hai giá trị mua/bán — `item_price_levels` vì
FR-SYS-042 nói thẳng "đơn giá mua **và** đơn giá bán", `price_lists` vì danh mục
đối tác gộp khách hàng với nhà cung cấp (FR-SYS-031) nên một bảng giá trỏ vào đối
tác không tự nói nó là chiều nào. Đặt bộ đọc ở `modules/sales` thì `modules/
purchase` **không import được** (C3 cấm module gọi module), nên chiều mua sẽ phải
có một bản sao — và hai bản sao của một luật định giá là hai bảng giá cho cùng
một câu hỏi. Ở kernel thì cả hai phân hệ gọi được, và ranh giới C1 vẫn nguyên:
tệp này chỉ đọc dữ liệu danh mục của chính kernel.

Bộ này **không** làm tròn thành tiền và không dựng dòng chứng từ — nó trả đơn giá
và tỷ lệ chiết khấu, còn nhân với số lượng rồi làm tròn là việc của service dựng
chứng từ, nơi biết `money.scale` của dữ liệu kế toán.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.orm import Mapped, Session

from ket.kernel.config.catalog import PRICE_IS_TAX_INCLUSIVE_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.master_data.models.item import Item
from ket.kernel.master_data.models.item_discount_tier import ItemDiscountTier
from ket.kernel.master_data.models.item_price_level import ItemPriceLevel, PriceDirection
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.price_list import PriceList
from ket.kernel.master_data.models.price_list_line import PriceListLine
from ket.kernel.money import UNIT_PRICE_SCALE, ZERO, divide_money, round_money

DEFAULT_PRICE_LEVEL = 1
"""Mức giá dùng khi người lập chứng từ không chọn mức nào."""

HUNDRED = Decimal(100)


class PriceSource(StrEnum):
    """Tầng đã trả lời câu hỏi giá — đi kèm kết quả để màn hình nói được vì sao.

    Người dùng thấy một đơn giá tự điền và câu hỏi đầu tiên của họ là "số này ở
    đâu ra". Trả về nguồn là cách rẻ nhất để trả lời mà không bắt họ mở ba màn
    hình khai báo.
    """

    PRICE_LIST = "price_list"
    """Từ một dòng bảng giá theo đối tác/hợp đồng."""
    ITEM_LEVEL = "item_level"
    """Từ mức giá của mã hàng theo đúng đơn vị của dòng."""
    ITEM_DEFAULT = "item_default"
    """Từ đơn giá mặc định trên danh mục (mức 1, theo đơn vị chính)."""
    NONE = "none"
    """Không tầng nào khai giá — người lập chứng từ gõ tay."""


@dataclass(frozen=True)
class QuotedPrice:
    """Kết quả định giá cho **một** dòng chứng từ."""

    unit_price: Decimal
    """Đơn giá **trước thuế**, tức con số đi vào dòng chứng từ.

    Đã tách thuế ngược nếu giá khai là giá sau thuế (FR-SYS-043/FR-SAL-024); bằng
    `quoted_price` nếu không."""

    quoted_price: Decimal
    """Đơn giá **đúng như đã khai** trong bảng giá — để màn hình hiện lại được con
    số người dùng nhớ, kể cả khi nó đã gồm thuế."""

    is_tax_inclusive: bool
    """Giá khai có gồm thuế GTGT hay không, sau khi đã hợp nhất cờ của mã hàng với
    thiết lập hệ thống."""

    source: PriceSource
    price_list_id: int | None
    """Bảng giá đã trả lời, khi `source` là `PRICE_LIST`."""

    level: int | None
    """Mức giá đã dùng, khi nguồn là một trong hai tầng `item_price_levels`."""

    discount_percent: Decimal
    """Tỷ lệ chiết khấu thương mại theo bậc số lượng (FR-SYS-045). `0` khi không
    bậc nào thỏa, khi mã hàng không khai bậc, hoặc khi chiều là **mua**."""


def price_is_tax_inclusive_default(session: Session, *, user_id: int) -> bool:
    """Vế "cấp hệ thống" của FR-SYS-043 — đọc **một lần cho cả transaction**.

    Tham số của `quote_price` chứ không phải một lượt đọc bên trong nó, đúng cảnh
    báo ghi ở `settings_service.value_of`: mỗi lời gọi là một lượt truy vấn, và
    định giá chạy **theo dòng** chứng từ. Đọc bên trong sẽ biến một hóa đơn 500
    dòng thành 500 lượt đọc tùy chọn cho một giá trị không đổi giữa chừng.
    """
    return value_of(session, key=PRICE_IS_TAX_INCLUSIVE_KEY, user_id=user_id) is True


def quote_price(
    session: Session,
    *,
    item_id: int,
    unit_id: int | None,
    quantity: Decimal,
    direction: PriceDirection,
    on_date: date,
    tax_inclusive_default: bool,
    partner_id: int | None = None,
    contract_id: int | None = None,
    price_list_id: int | None = None,
    level: int = DEFAULT_PRICE_LEVEL,
    tax_rate: Decimal = ZERO,
) -> QuotedPrice:
    """Đơn giá và chiết khấu cho một dòng, theo thứ tự ba tầng ở đầu tệp.

    `unit_id=None` nghĩa "đơn vị chính của mã hàng", cùng quy ước với hai bảng giá.

    `price_list_id` khi có giá trị là **ép dùng đúng bảng giá ấy** — người lập
    chứng từ chọn tay trên form (`sales_invoices.price_policy_id`). Ép chứ không
    "ưu tiên": nếu bảng giá được chọn không có dòng cho mã hàng này thì rơi thẳng
    xuống tầng 2, không đi tìm bảng giá khác. Một lựa chọn tường minh mà hệ thống
    lặng lẽ thay bằng lựa chọn khác là lựa chọn không ai tin được nữa.

    `tax_rate` là thuế suất GTGT của dòng, tính bằng **phần trăm** (`10` chứ không
    `0.1`) — chỉ dùng khi giá khai là giá sau thuế.

    `unit_id` **trùng đơn vị chính của mã hàng được quy về `None`** ngay đầu — xem
    `_normalized_unit`.
    """
    unit_id = _normalized_unit(session, item_id=item_id, unit_id=unit_id)
    quoted = _quote_from_price_list(
        session,
        item_id=item_id,
        unit_id=unit_id,
        quantity=quantity,
        direction=direction,
        on_date=on_date,
        partner_id=partner_id,
        contract_id=contract_id,
        price_list_id=price_list_id,
    ) or _quote_from_item_levels(
        session, item_id=item_id, unit_id=unit_id, direction=direction, level=level
    )
    price, source, list_id, used_level = quoted

    is_tax_inclusive = _tax_inclusive_for(session, item_id=item_id, default=tax_inclusive_default)
    unit_price = _strip_tax(price, tax_rate) if is_tax_inclusive else price

    return QuotedPrice(
        unit_price=unit_price,
        quoted_price=price,
        is_tax_inclusive=is_tax_inclusive,
        source=source,
        price_list_id=list_id,
        level=used_level,
        discount_percent=(
            discount_percent_for(session, item_id=item_id, unit_id=unit_id, quantity=quantity)
            if direction is PriceDirection.SALE
            else ZERO
        ),
    )


def _normalized_unit(session: Session, *, item_id: int, unit_id: int | None) -> int | None:
    """Đưa `unit_id` về đúng quy ước của hai bảng giá: đơn vị chính viết là `None`.

    Hai bên của đường này dùng **hai quy ước khác nhau**, và bắc cầu giữa chúng là
    việc của đường đọc:

    * **Bảng giá** viết đơn vị chính bằng `NULL` — đơn vị chính không có dòng
      `item_units` nào (tỷ lệ của nó luôn là 1), nên gửi id của nó là một cách viết
      thứ hai cho cùng một dòng, và đường **ghi** từ chối đúng vì lẽ đó
      (`ensure_unit_is_priceable`).
    * **Dòng chứng từ** viết đơn vị bằng id thật: `purchase_invoice_lines.unit_id`
      (lát 7B) là một `INTEGER` tự do, và một dòng bán theo đơn vị chính mang id
      của đơn vị chính, không mang `NULL`.

    Không quy ước lại ở đây thì ca **thường gặp nhất** — bán theo đơn vị chính —
    trượt cả ba tầng giá **và** bậc chiết khấu, trả về đơn giá 0 trong im lặng
    (review H-2). Đường ghi vẫn từ chối id ấy: ghi cần đúng một cách viết, còn đọc
    phải nhận được thứ chứng từ thật sự mang.
    """
    if unit_id is None:
        return None
    base_unit_id = session.execute(
        select(Item.base_unit_id).where(Item.id == item_id)
    ).scalar_one_or_none()
    return None if base_unit_id == unit_id else unit_id


def discount_percent_for(
    session: Session, *, item_id: int, unit_id: int | None, quantity: Decimal
) -> Decimal:
    """Bậc chiết khấu áp cho một số lượng (FR-SYS-045).

    Bậc áp là bậc có `min_quantity` **lớn nhất** mà vẫn `≤` số lượng, sau khi quy
    số lượng của dòng về **đơn vị chính** — ngưỡng khai theo đơn vị chính (xem
    `models/item_discount_tier.py`), nên không quy đổi trước khi so thì cùng một
    bảng bậc cho ra ưu đãi khác nhau tùy người nhập gõ "2 thùng" hay "48 chiếc".

    Không bậc nào thỏa thì `0`, không phải lỗi: bảng bậc là ưu đãi, không phải
    điều kiện bán hàng.

    Tự quy `unit_id` về quy ước bảng giá (`_normalized_unit`) chứ không tin nơi gọi
    đã làm: hàm này công khai, và một dòng chứng từ mang id của **đơn vị chính** sẽ
    không tra được tỷ lệ quy đổi nào — bậc chiết khấu im lặng thành 0.
    """
    unit_id = _normalized_unit(session, item_id=item_id, unit_id=unit_id)
    quantity_base = _to_base_quantity(session, item_id=item_id, unit_id=unit_id, quantity=quantity)
    if quantity_base is None:
        return ZERO
    percent = session.execute(
        select(ItemDiscountTier.discount_percent)
        .where(
            ItemDiscountTier.item_id == item_id,
            ItemDiscountTier.min_quantity <= quantity_base,
        )
        .order_by(ItemDiscountTier.min_quantity.desc())
        .limit(1)
    ).scalar_one_or_none()
    return percent if percent is not None else ZERO


def _quote_from_price_list(
    session: Session,
    *,
    item_id: int,
    unit_id: int | None,
    quantity: Decimal,
    direction: PriceDirection,
    on_date: date,
    partner_id: int | None,
    contract_id: int | None,
    price_list_id: int | None,
) -> tuple[Decimal, PriceSource, int | None, int | None] | None:
    """Tầng 1 — dòng bảng giá hẹp nhất còn hiệu lực, hoặc `None`."""
    for candidate_id in _candidate_price_lists(
        session,
        direction=direction,
        on_date=on_date,
        partner_id=partner_id,
        contract_id=contract_id,
        price_list_id=price_list_id,
    ):
        price = session.execute(
            _unit_filtered(
                select(PriceListLine.price).where(
                    PriceListLine.price_list_id == candidate_id,
                    PriceListLine.item_id == item_id,
                    PriceListLine.min_quantity <= quantity,
                ),
                PriceListLine.unit_id,
                unit_id,
            )
            .order_by(PriceListLine.min_quantity.desc())
            .limit(1)
        ).scalar_one_or_none()
        if price is not None:
            return price, PriceSource.PRICE_LIST, candidate_id, None
    return None


def _candidate_price_lists(
    session: Session,
    *,
    direction: PriceDirection,
    on_date: date,
    partner_id: int | None,
    contract_id: int | None,
    price_list_id: int | None,
) -> list[int]:
    """Bảng giá còn hiệu lực khớp phạm vi, **hẹp nhất trước**.

    Người lập chứng từ chọn tay một bảng giá thì danh sách chỉ có nó — xem
    `quote_price`.

    Thứ tự phân xử khi không ai chọn tay, từ hẹp tới rộng:

    1. khớp **hợp đồng**;
    2. khớp **đúng đối tác**;
    3. khớp một **nút nhóm tổ tiên** của đối tác, nhóm **sâu hơn trước** — "khách
       VIP miền Bắc" hẹp hơn "khách VIP";
    4. bảng giá **chung** (không trỏ đối tác, không trỏ hợp đồng).

    Độ cụ thể đọc từ chính dữ liệu chứ không từ một cột ưu tiên gõ tay — xem đầu
    `models/price_list.py`. Cây đối tác cho sẵn thứ hạng ở bậc 3: `path` là chuỗi
    id từ gốc tới lá, nên **vị trí trong `path`** đúng bằng độ sâu của nút.

    Hai bảng giá cùng độ cụ thể (cùng trỏ một đối tác, cùng còn hiệu lực) thì lấy
    bảng có `effective_from` **muộn hơn** — bảng giá mới ban hành đè bảng cũ chưa
    kịp đóng hạn, đúng cách người dùng nghĩ về một lần điều chỉnh giá. `id` giảm
    dần là chốt chặn cuối để kết quả **tất định**: hai bảng giá giống hệt nhau tới
    từng cột thì vẫn phải có một câu trả lời, và một câu trả lời đổi theo thứ tự
    quét của Postgres là thứ không ai gỡ được khi nó sai.
    """
    if price_list_id is not None:
        # Vẫn đi qua **đúng bộ điều kiện** của `_price_list_query`, không trả thẳng
        # id: "ép dùng bảng giá này" nghĩa là bỏ qua phép **đi tìm**, không phải bỏ
        # qua phép **kiểm**. Trả thẳng id thì một bảng giá đã hết hạn, đã ngừng
        # theo dõi, sai chiều hay là nút nhóm đều áp được — đúng bốn ca mà đường
        # tự tìm đã chặn, lọt qua một đường khác (review C-2).
        return list(
            session.execute(_price_list_query(direction, on_date, PriceList.id == price_list_id))
            .scalars()
            .all()
        )

    ancestor_ids = _partner_ancestry(session, partner_id)
    scopes: list[tuple[int, Select[tuple[int]]]] = []
    if contract_id is not None:
        # Bảng giá của hợp đồng **và** của một đối tác khác không được khớp: nó
        # hẹp theo hai trục, nên chỉ trục hợp đồng khớp là chưa đủ. `partner_id`
        # để trống thì bảng giá ấy áp cho mọi bên ký hợp đồng này.
        scopes.append(
            (
                0,
                _price_list_query(
                    direction,
                    on_date,
                    and_(
                        PriceList.contract_id == contract_id,
                        or_(
                            PriceList.partner_id.is_(None),
                            PriceList.partner_id.in_(ancestor_ids),
                        ),
                    ),
                ),
            )
        )
    for depth, ancestor_id in enumerate(reversed(ancestor_ids)):
        scopes.append(
            (
                1 + depth,
                _price_list_query(
                    direction,
                    on_date,
                    and_(PriceList.partner_id == ancestor_id, PriceList.contract_id.is_(None)),
                ),
            )
        )
    scopes.append(
        (
            1 + len(ancestor_ids),
            _price_list_query(
                direction,
                on_date,
                and_(PriceList.partner_id.is_(None), PriceList.contract_id.is_(None)),
            ),
        )
    )

    ordered: list[int] = []
    for _, query in sorted(scopes, key=lambda pair: pair[0]):
        ordered.extend(session.execute(query).scalars().all())
    return ordered


def _price_list_query(
    direction: PriceDirection, on_date: date, scope: ColumnElement[bool]
) -> Select[tuple[int]]:
    """Bảng giá của một phạm vi, còn theo dõi và còn hiệu lực vào `on_date`."""
    return (
        select(PriceList.id)
        .where(
            PriceList.direction == direction,
            PriceList.is_active.is_(True),
            PriceList.is_group.is_(False),
            or_(PriceList.effective_from.is_(None), PriceList.effective_from <= on_date),
            or_(PriceList.effective_to.is_(None), PriceList.effective_to >= on_date),
            scope,
        )
        .order_by(PriceList.effective_from.desc().nulls_last(), PriceList.id.desc())
    )


def _partner_ancestry(session: Session, partner_id: int | None) -> list[int]:
    """Id của đối tác kèm mọi nút nhóm tổ tiên, **gốc trước lá sau**.

    Đọc `MasterDataRow.path` — chuỗi id ngăn bằng dấu chấm mà lát 3A dựng sẵn cho
    đúng loại câu hỏi này — thay vì đệ quy lên `parent_id`: một lượt đọc thay cho
    một vòng lặp truy vấn theo độ sâu cây.
    """
    if partner_id is None:
        return []
    path = session.execute(
        select(Partner.path).where(Partner.id == partner_id)
    ).scalar_one_or_none()
    if path is None:
        return []
    return [int(part) for part in path.split(".") if part]


def _quote_from_item_levels(
    session: Session,
    *,
    item_id: int,
    unit_id: int | None,
    direction: PriceDirection,
    level: int,
) -> tuple[Decimal, PriceSource, int | None, int | None]:
    """Tầng 2 rồi tầng 3 — mức giá theo đúng đơn vị, rồi đơn giá mặc định."""
    price = session.execute(
        _unit_filtered(
            select(ItemPriceLevel.price).where(
                ItemPriceLevel.item_id == item_id,
                ItemPriceLevel.direction == direction,
                ItemPriceLevel.level == level,
            ),
            ItemPriceLevel.unit_id,
            unit_id,
        )
    ).scalar_one_or_none()
    if price is not None:
        return price, PriceSource.ITEM_LEVEL, None, level

    # Tầng 3: đơn giá mặc định trên danh mục — mức 1 theo đơn vị chính. Bỏ qua khi
    # dòng vốn đã hỏi đúng ô ấy, nếu không nguồn trả về sẽ nói sai tầng nào đã trả
    # lời cho cùng một truy vấn vừa trượt.
    if unit_id is None and level == DEFAULT_PRICE_LEVEL:
        return ZERO, PriceSource.NONE, None, None
    default_price = session.execute(
        select(ItemPriceLevel.price).where(
            ItemPriceLevel.item_id == item_id,
            ItemPriceLevel.direction == direction,
            ItemPriceLevel.level == DEFAULT_PRICE_LEVEL,
            ItemPriceLevel.unit_id.is_(None),
        )
    ).scalar_one_or_none()
    if default_price is None:
        return ZERO, PriceSource.NONE, None, None

    converted = _in_requested_unit(session, item_id=item_id, unit_id=unit_id, price=default_price)
    if converted is None:
        return ZERO, PriceSource.NONE, None, None
    return converted, PriceSource.ITEM_DEFAULT, None, DEFAULT_PRICE_LEVEL


def _in_requested_unit(
    session: Session, *, item_id: int, unit_id: int | None, price: Decimal
) -> Decimal | None:
    """Đơn giá mặc định (theo **đơn vị chính**) quy về đơn vị của dòng chứng từ.

    Đây là phép nhân mà bản đầu tiên của lát này **quên**, và hậu quả không lộ ra
    ở bất kỳ phép kiểm nào: khai `12.000/lon`, bán 5 thùng 24 lon, tầng 3 trả
    `12.000` làm đơn giá **một thùng** — hóa đơn ra 60.000đ thay vì 1.440.000đ,
    kèm `source = item_default` nói rằng con số ấy có căn cứ (review C-1).

    Quy đổi bằng `item_units.factor` = số đơn vị chính cho một đơn vị này, đúng
    chiều mà `models/item_unit.py` chốt: **luôn nhân, không bao giờ chia**. Giá một
    thùng = giá một lon × 24.

    `None` khi mã hàng chưa khai tỷ lệ cho đơn vị ấy: không có đường nào quy đổi
    thì không có đơn giá nào để trả, và người lập chứng từ gõ tay — cùng câu trả
    lời an toàn với `_to_base_quantity`.

    Quyết định user 2026-09-04: **có** bắc cầu qua đơn vị, tức chấp nhận giả định
    giá tỉ lệ tuyến tính theo quy cách đóng gói. Đơn vị nào bán không theo tỉ lệ ấy
    (bán sỉ một thùng rẻ hơn 24 lần giá lẻ) thì khai một dòng `item_price_levels`
    riêng cho nó — tầng 2 đứng trước tầng 3 nên dòng ấy luôn thắng.
    """
    if unit_id is None:
        return price
    factor = session.execute(
        select(ItemUnit.factor).where(ItemUnit.item_id == item_id, ItemUnit.unit_id == unit_id)
    ).scalar_one_or_none()
    if factor is None:
        return None
    return round_money(price * factor, UNIT_PRICE_SCALE)


def _tax_inclusive_for(session: Session, *, item_id: int, default: bool) -> bool:
    """Hợp nhất cờ ba trạng thái của mã hàng với thiết lập hệ thống (FR-SYS-043).

    Cờ của mã hàng thắng khi nó có câu trả lời; `NULL` là "theo hệ thống", tức
    `default` mà nơi gọi đã đọc một lần qua `price_is_tax_inclusive_default`.
    """
    override = session.execute(
        select(Item.price_is_tax_inclusive).where(Item.id == item_id)
    ).scalar_one_or_none()
    return default if override is None else override


def _strip_tax(price: Decimal, tax_rate: Decimal) -> Decimal:
    """Tách thuế ngược khỏi một đơn giá đã gồm thuế: `giá / (1 + thuế_suất)`.

    Thuế suất `0` (hàng không chịu thuế, hoặc dòng chưa chọn thuế suất) thì đơn
    giá sau thuế **bằng** đơn giá trước thuế — phép chia cho 1, không phải một ca
    đặc biệt.

    Làm tròn về `UNIT_PRICE_SCALE` chứ không về số chữ số của tiền: kết quả này là
    một **đơn giá**, và nó còn được nhân với số lượng trước khi thành tiền — làm
    tròn sớm về đồng ở đây là đẩy sai số lên gấp `số_lượng` lần.
    """
    if tax_rate == ZERO:
        return price
    return divide_money(price, Decimal(1) + tax_rate / HUNDRED, scale=UNIT_PRICE_SCALE)


def _to_base_quantity(
    session: Session, *, item_id: int, unit_id: int | None, quantity: Decimal
) -> Decimal | None:
    """Số lượng quy về đơn vị chính; `None` khi đơn vị không quy đổi được.

    `None` chứ không ném lỗi: dòng chứng từ ghi bằng một đơn vị mã hàng chưa khai
    tỷ lệ là dữ liệu đã lọt vào từ trước (nhập Excel, dữ liệu chuyển đổi), và một
    bậc chiết khấu không áp được là câu trả lời an toàn hơn một chứng từ không lập
    được.
    """
    if unit_id is None:
        return quantity
    factor = session.execute(
        select(ItemUnit.factor).where(ItemUnit.item_id == item_id, ItemUnit.unit_id == unit_id)
    ).scalar_one_or_none()
    return None if factor is None else quantity * factor


def _unit_filtered(
    query: Select[tuple[Decimal]], column: Mapped[int | None], unit_id: int | None
) -> Select[tuple[Decimal]]:
    """Thêm điều kiện đơn vị, phân biệt `NULL` với một id cụ thể.

    `= NULL` không bao giờ đúng trong SQL, nên "giá theo đơn vị chính" phải hỏi
    bằng `IS NULL`. Gói lại một chỗ vì cả hai bảng giá hỏi cùng câu này, và một
    trong hai chỗ quên `IS NULL` sẽ lặng lẽ **không tìm thấy giá nào** thay vì báo
    lỗi.
    """
    return query.where(column.is_(None) if unit_id is None else column == unit_id)
