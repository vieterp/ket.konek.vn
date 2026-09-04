"""Bộ định giá: ba tầng nguồn giá, bậc chiết khấu, tách thuế ngược (lát 7C-1).

Đi thẳng qua `session` chứ không qua HTTP: thứ đang đo là **luật chọn giá**, và
mỗi bài cần dựng vài bản ghi danh mục ở đúng một hình dạng. Đường HTTP của cùng
những bảng ấy có bộ test riêng ở `test_item_pricing_api.py`.

Ba tầng, theo đúng thứ tự `kernel/pricing` xét:

1. bảng giá theo đối tác/hợp đồng (FR-SAL-020),
2. mức giá của mã hàng theo đúng đơn vị (FR-SYS-042),
3. đơn giá mặc định trên danh mục — mức 1, `unit_id IS NULL`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import unique_code
from ket.kernel.config.catalog import PRICE_IS_TAX_INCLUSIVE_KEY, SettingScope
from ket.kernel.config.settings_service import set_setting
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.item_discount_tier_service import ItemDiscountTierService
from ket.kernel.master_data.item_price_level_service import ItemPriceLevelService
from ket.kernel.master_data.item_unit_service import ItemUnitService
from ket.kernel.master_data.models.contract import Contract
from ket.kernel.master_data.models.item import Item, ItemNature
from ket.kernel.master_data.models.item_price_level import PriceDirection
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.price_list import PriceList
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.price_list_line_service import PriceListLineService
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.pricing import (
    DEFAULT_PRICE_LEVEL,
    PriceSource,
    QuotedPrice,
    discount_percent_for,
    price_is_tax_inclusive_default,
    quote_price,
)

pytestmark = pytest.mark.db

ON_DATE = date(2026, 6, 15)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


class _World:
    """Bối cảnh tối thiểu của một bài: một mã hàng, đơn vị chính, một đơn vị quy đổi.

    Lớp mỏng thay vì sáu tham số trả về: mỗi bài chỉ dùng vài mảnh, và một tuple
    sáu phần tử là sáu chỗ để đọc nhầm thứ tự.
    """

    def __init__(self, session: Session) -> None:
        units: MasterDataService[UnitOfMeasure] = MasterDataService(session, UnitOfMeasure)
        items: MasterDataService[Item] = MasterDataService(session, Item)
        self.session = session
        self.base_unit = units.create(code=unique_code("DVC"), name="Chiếc")
        self.box_unit = units.create(code=unique_code("DVT"), name="Thùng")
        self.item = items.create(
            code=unique_code("VT"),
            name="Hàng có giá",
            extra={"nature": ItemNature.GOODS, "base_unit_id": self.base_unit.id},
        )
        # Thùng = 24 chiếc — tỷ lệ thật để bậc chiết khấu có gì để quy đổi.
        ItemUnitService(session).add(
            item_id=self.item.id, unit_id=self.box_unit.id, factor=Decimal(24)
        )

    def level(
        self,
        price: str,
        *,
        unit_id: int | None = None,
        level: int = DEFAULT_PRICE_LEVEL,
        direction: PriceDirection = PriceDirection.SALE,
    ) -> None:
        ItemPriceLevelService(self.session).add(
            item_id=self.item.id,
            unit_id=unit_id,
            direction=direction,
            level=level,
            price=Decimal(price),
        )

    def price_list(
        self,
        *,
        partner_id: int | None = None,
        contract_id: int | None = None,
        effective_from: date | None = None,
        effective_to: date | None = None,
        is_active: bool = True,
    ) -> PriceList:
        lists: MasterDataService[PriceList] = MasterDataService(self.session, PriceList)
        row = lists.create(
            code=unique_code("BG"),
            name="Bảng giá",
            extra={
                "direction": PriceDirection.SALE,
                "partner_id": partner_id,
                "contract_id": contract_id,
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
        )
        if not is_active:
            row.is_active = False
            self.session.flush()
        return row

    def line(
        self,
        price_list: PriceList,
        price: str,
        *,
        unit_id: int | None = None,
        min_quantity: str = "1",
    ) -> None:
        PriceListLineService(self.session).add(
            price_list_id=price_list.id,
            item_id=self.item.id,
            unit_id=unit_id,
            min_quantity=Decimal(min_quantity),
            price=Decimal(price),
        )

    def quote(
        self,
        *,
        quantity: str = "1",
        unit_id: int | None = None,
        level: int = DEFAULT_PRICE_LEVEL,
        partner_id: int | None = None,
        contract_id: int | None = None,
        price_list_id: int | None = None,
        tax_rate: str = "0",
        tax_inclusive_default: bool = False,
        direction: PriceDirection = PriceDirection.SALE,
    ) -> QuotedPrice:
        return quote_price(
            self.session,
            item_id=self.item.id,
            unit_id=unit_id,
            quantity=Decimal(quantity),
            direction=direction,
            on_date=ON_DATE,
            tax_inclusive_default=tax_inclusive_default,
            partner_id=partner_id,
            contract_id=contract_id,
            price_list_id=price_list_id,
            level=level,
            tax_rate=Decimal(tax_rate),
        )


# ------------------------------------------------------------ ba tầng nguồn giá


def test_an_item_without_any_price_quotes_zero_instead_of_failing(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chưa khai giá là chuyện thường ngày, không phải lỗi.

    Ném lỗi ở đây sẽ biến một ô trống thành một cái chặn: người lập chứng từ gõ
    tay đơn giá là đường hợp lệ.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        quoted = _World(session).quote()
        assert quoted.source is PriceSource.NONE
        assert quoted.unit_price == Decimal(0)


def test_the_catalog_default_answers_when_nothing_narrower_exists(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tầng 3: mức 1 theo đơn vị chính **là** "đơn giá mặc định trên danh mục"."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")

        quoted = world.quote()
        assert quoted.source is PriceSource.ITEM_LEVEL
        assert quoted.unit_price == Decimal(1000)
        assert quoted.level == DEFAULT_PRICE_LEVEL


def test_a_level_for_the_requested_unit_beats_the_catalog_default(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tầng 2 trước tầng 3: giá theo thùng thắng giá theo đơn vị chính."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")
        world.level("22000", unit_id=world.box_unit.id)

        assert world.quote(unit_id=world.box_unit.id).unit_price == Decimal(22000)


def test_a_missing_level_falls_back_to_the_catalog_default(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hỏi mức 2 mà mã hàng chỉ khai mức 1 theo đơn vị chính: rơi xuống tầng 3.

    Nguồn trả về phải nói đúng tầng nào đã trả lời — nếu không, màn hình sẽ khoe
    một "mức 2" mà bảng giá không hề có.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")

        quoted = world.quote(level=2)
        assert quoted.source is PriceSource.ITEM_DEFAULT
        assert quoted.unit_price == Decimal(1000)
        assert quoted.level == DEFAULT_PRICE_LEVEL


def test_a_price_list_beats_every_item_level(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tầng 1 trước tất cả — kể cả bảng giá **chung** không trỏ đối tác nào."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")
        world.line(world.price_list(), "900")

        quoted = world.quote()
        assert quoted.source is PriceSource.PRICE_LIST
        assert quoted.unit_price == Decimal(900)


def test_a_partner_price_list_beats_the_general_one(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hẹp hơn thì thắng: bảng giá của đúng khách hàng đè bảng giá chung."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        customer = partners.create(
            code=unique_code("KH"), name="Khách lẻ", extra={"is_customer": True}
        )
        world.line(world.price_list(), "900")
        world.line(world.price_list(partner_id=customer.id), "850")

        assert world.quote(partner_id=customer.id).unit_price == Decimal(850)


def test_a_price_list_of_the_partner_group_applies_to_its_members(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """ "Nhóm khách hàng" của FR-SAL-020 = một nút nhóm trong cây đối tác.

    Không cần cột thứ hai: bộ định giá leo `path` — chuỗi id từ gốc tới lá mà lát
    3A dựng sẵn cho đúng loại câu hỏi này.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        group = partners.create(code=unique_code("NKH"), name="Khách VIP", is_group=True)
        member = partners.create(
            code=unique_code("KH"),
            name="Khách VIP miền Bắc",
            parent_id=group.id,
            extra={"is_customer": True},
        )
        world.line(world.price_list(), "900")
        world.line(world.price_list(partner_id=group.id), "800")

        assert world.quote(partner_id=member.id).unit_price == Decimal(800)


def test_the_deeper_partner_group_wins_over_the_shallower_one(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Độ cụ thể đọc từ **vị trí trong `path`**, không từ một cột ưu tiên gõ tay."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        top = partners.create(code=unique_code("NKH"), name="Khách VIP", is_group=True)
        inner = partners.create(
            code=unique_code("NKH"), name="VIP miền Bắc", is_group=True, parent_id=top.id
        )
        member = partners.create(
            code=unique_code("KH"),
            name="Khách",
            parent_id=inner.id,
            extra={"is_customer": True},
        )
        world.line(world.price_list(partner_id=top.id), "800")
        world.line(world.price_list(partner_id=inner.id), "780")

        assert world.quote(partner_id=member.id).unit_price == Decimal(780)


def test_a_price_list_outside_its_effective_window_is_ignored(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hết hạn thì không áp — và ngày cuối cửa sổ **tính cả ngày đó**."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")
        world.line(world.price_list(effective_to=date(2026, 6, 14)), "900")

        assert world.quote().source is PriceSource.ITEM_LEVEL

        world.line(world.price_list(effective_to=ON_DATE), "880")
        assert world.quote().unit_price == Decimal(880)


def test_a_price_list_that_is_no_longer_tracked_is_ignored(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """ "Ngừng theo dõi" (FR-SYS-012) phải có tác dụng ở đường đọc giá."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")
        world.line(world.price_list(is_active=False), "900")

        assert world.quote().source is PriceSource.ITEM_LEVEL


def test_choosing_a_price_list_by_hand_does_not_fall_back_to_another_one(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chọn tay là **ép**, không phải "ưu tiên".

    Bảng giá được chọn không có dòng cho mã hàng này thì rơi thẳng xuống tầng 2 —
    một lựa chọn tường minh mà hệ thống lặng lẽ thay bằng lựa chọn khác là lựa
    chọn không ai tin được nữa.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")
        world.line(world.price_list(), "900")
        empty = world.price_list()

        quoted = world.quote(price_list_id=empty.id)
        assert quoted.source is PriceSource.ITEM_LEVEL
        assert quoted.unit_price == Decimal(1000)


def test_the_price_list_line_with_the_largest_threshold_below_the_quantity_wins(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Trục "số lượng" của FR-SAL-020: ngưỡng lớn nhất mà vẫn ≤ số lượng."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        price_list = world.price_list()
        world.line(price_list, "1000")
        world.line(price_list, "900", min_quantity="10")
        world.line(price_list, "800", min_quantity="100")

        assert world.quote(quantity="9").unit_price == Decimal(1000)
        assert world.quote(quantity="10").unit_price == Decimal(900)
        assert world.quote(quantity="99").unit_price == Decimal(900)
        assert world.quote(quantity="100").unit_price == Decimal(800)


# ----------------------------------------------------------- bậc chiết khấu


def test_the_discount_tier_is_chosen_by_quantity_in_the_base_unit(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ngưỡng khai theo đơn vị chính, nên "2 thùng" và "48 chiếc" phải ra cùng bậc.

    Không quy đổi trước khi so thì cùng một bảng bậc cho ra ưu đãi khác nhau tùy
    người nhập gõ đơn vị nào — hai cách viết của cùng một lượt mua.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        ItemDiscountTierService(session).add(
            item_id=world.item.id, min_quantity=Decimal(48), discount_percent=Decimal(5)
        )

        by_box = discount_percent_for(
            session, item_id=world.item.id, unit_id=world.box_unit.id, quantity=Decimal(2)
        )
        by_piece = discount_percent_for(
            session, item_id=world.item.id, unit_id=None, quantity=Decimal(48)
        )
        assert by_box == by_piece == Decimal(5)


def test_a_quantity_below_every_tier_gets_no_discount_and_no_error(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bảng bậc là ưu đãi, không phải điều kiện bán hàng."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        ItemDiscountTierService(session).add(
            item_id=world.item.id, min_quantity=Decimal(10), discount_percent=Decimal(5)
        )

        assert world.quote(quantity="9").discount_percent == Decimal(0)


def test_the_highest_matching_tier_wins(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        service = ItemDiscountTierService(session)
        service.add(item_id=world.item.id, min_quantity=Decimal(10), discount_percent=Decimal(2))
        service.add(item_id=world.item.id, min_quantity=Decimal(50), discount_percent=Decimal(7))

        assert world.quote(quantity="50").discount_percent == Decimal(7)


def test_a_purchase_quote_never_carries_a_sales_discount_tier(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """FR-SYS-045 nói "tự áp dụng khi lập chứng từ **bán**"."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        ItemDiscountTierService(session).add(
            item_id=world.item.id, min_quantity=Decimal(1), discount_percent=Decimal(5)
        )
        world.level("700", direction=PriceDirection.PURCHASE)

        quoted = world.quote(quantity="10", direction=PriceDirection.PURCHASE)
        assert quoted.unit_price == Decimal(700)
        assert quoted.discount_percent == Decimal(0)


def test_a_sale_quote_does_not_see_a_purchase_price(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chiều là một trục thật của bảng giá, không phải nhãn trang trí."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("700", direction=PriceDirection.PURCHASE)

        assert world.quote().source is PriceSource.NONE


# ------------------------------------------------------ giá sau thuế (FR-SYS-043)


def test_a_tax_inclusive_price_is_split_back_before_it_reaches_the_line(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`đơn_giá_trước_thuế = đơn_giá / (1 + thuế_suất)`, giữ nguyên số đã khai."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("110")

        quoted = world.quote(tax_rate="10", tax_inclusive_default=True)
        assert quoted.is_tax_inclusive
        assert quoted.quoted_price == Decimal(110)
        assert quoted.unit_price == Decimal(100)


def test_a_zero_tax_rate_leaves_a_tax_inclusive_price_untouched(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hàng không chịu thuế: chia cho 1, không phải một ca đặc biệt."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("110")

        quoted = world.quote(tax_inclusive_default=True)
        assert quoted.unit_price == Decimal(110)


def test_the_item_flag_overrides_the_system_setting_in_both_directions(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ba trạng thái: `NULL` theo hệ thống, `TRUE`/`FALSE` ghi đè cho riêng mã hàng."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("110")

        world.item.price_is_tax_inclusive = False
        session.flush()
        assert not world.quote(tax_rate="10", tax_inclusive_default=True).is_tax_inclusive

        world.item.price_is_tax_inclusive = True
        session.flush()
        assert world.quote(tax_rate="10", tax_inclusive_default=False).is_tax_inclusive


def test_the_system_setting_is_what_a_null_item_flag_follows(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Vế "cấp hệ thống" của FR-SYS-043, đọc qua đúng khóa thiết lập.

    Đọc **một lần cho cả transaction** — `price_is_tax_inclusive_default` là tham
    số của `quote_price` chứ không một lượt đọc bên trong nó, vì định giá chạy
    theo dòng chứng từ.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("110")
        assert not price_is_tax_inclusive_default(session, user_id=1)

        set_setting(
            session,
            key=PRICE_IS_TAX_INCLUSIVE_KEY,
            scope=SettingScope.SYSTEM,
            user_id=1,
            raw_value="true",
            expected_row_version=None,
        )
        session.flush()

        assert price_is_tax_inclusive_default(session, user_id=1)
        quoted = world.quote(
            tax_rate="10",
            tax_inclusive_default=price_is_tax_inclusive_default(session, user_id=1),
        )
        assert quoted.unit_price == Decimal(100)


def test_a_contract_price_list_of_another_partner_does_not_apply(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bảng giá hẹp theo **hai** trục thì chỉ khớp một trục là chưa đủ.

    Một bảng giá gắn hợp đồng H và khách A không được áp cho khách B chỉ vì
    chứng từ của B cũng trỏ hợp đồng H.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        contracts: MasterDataService[Contract] = MasterDataService(session, Contract)
        first = partners.create(code=unique_code("KH"), name="Khách A", extra={"is_customer": True})
        second = partners.create(
            code=unique_code("KH"), name="Khách B", extra={"is_customer": True}
        )
        contract = contracts.create(code=unique_code("HD"), name="Hợp đồng khung")
        world.level("1000")
        world.line(world.price_list(partner_id=first.id, contract_id=contract.id), "700")

        for_first = world.quote(partner_id=first.id, contract_id=contract.id)
        assert for_first.unit_price == Decimal(700)

        for_second = world.quote(partner_id=second.id, contract_id=contract.id)
        assert for_second.source is PriceSource.ITEM_LEVEL
        assert for_second.unit_price == Decimal(1000)


def test_a_contract_price_list_without_a_partner_applies_to_every_signatory(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Để trống đối tác = áp cho mọi bên ký hợp đồng này."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        contracts: MasterDataService[Contract] = MasterDataService(session, Contract)
        customer = partners.create(
            code=unique_code("KH"), name="Khách", extra={"is_customer": True}
        )
        contract = contracts.create(code=unique_code("HD"), name="Hợp đồng khung")
        world.level("1000")
        world.line(world.price_list(contract_id=contract.id), "700")

        quoted = world.quote(partner_id=customer.id, contract_id=contract.id)
        assert quoted.unit_price == Decimal(700)


def test_a_contract_price_list_beats_the_partner_one(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hợp đồng hẹp hơn đối tác — độ cụ thể quyết định, không phải một cột ưu tiên."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        partners: MasterDataService[Partner] = MasterDataService(session, Partner)
        contracts: MasterDataService[Contract] = MasterDataService(session, Contract)
        customer = partners.create(
            code=unique_code("KH"), name="Khách", extra={"is_customer": True}
        )
        contract = contracts.create(code=unique_code("HD"), name="Hợp đồng khung")
        world.line(world.price_list(partner_id=customer.id), "850")
        world.line(world.price_list(contract_id=contract.id), "700")

        assert world.quote(partner_id=customer.id, contract_id=contract.id).unit_price == Decimal(
            700
        )


# ------------------------------------------ ca đã lọt vòng review (7C-1 pre-landing)


def test_a_line_in_a_conversion_unit_gets_the_default_price_scaled_by_the_factor(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tầng 3 phải **quy đổi**, không trả thẳng giá của đơn vị chính (review C-1).

    Khai 12.000/lon, thùng = 24 lon, bán 5 thùng. Bản đầu tiên trả `12.000` làm
    đơn giá **một thùng** — hóa đơn 60.000đ thay vì 1.440.000đ — và còn nói
    `source = item_default`, tức con số ấy có căn cứ.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("12000")

        quoted = world.quote(unit_id=world.box_unit.id, quantity="5")
        assert quoted.source is PriceSource.ITEM_DEFAULT
        assert quoted.unit_price == Decimal(12000) * 24


def test_a_declared_price_for_the_unit_beats_the_scaled_default(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bán sỉ một thùng rẻ hơn 24 lần giá lẻ: khai một dòng cho thùng là đủ.

    Tầng 2 đứng trước tầng 3 nên dòng ấy luôn thắng phép quy đổi tuyến tính — đó
    là lối thoát cho giả định mà quyết định "quy đổi theo `factor`" mang theo.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("12000")
        world.level("250000", unit_id=world.box_unit.id)

        assert world.quote(unit_id=world.box_unit.id).unit_price == Decimal(250000)


def test_an_unconvertible_unit_quotes_nothing_instead_of_a_wrong_number(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Không có tỷ lệ quy đổi thì không có đơn giá nào để trả."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        units: MasterDataService[UnitOfMeasure] = MasterDataService(session, UnitOfMeasure)
        stranger = units.create(code=unique_code("DVL"), name="Kiện")
        world.level("12000")

        assert world.quote(unit_id=stranger.id).source is PriceSource.NONE


def test_naming_the_base_unit_explicitly_still_finds_every_price(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Dòng chứng từ mang **id thật** của đơn vị chính, bảng giá viết nó là `NULL`.

    Đường đọc bắc cầu giữa hai quy ước (`_normalized_unit`). Không có nó thì ca
    **thường gặp nhất** — bán theo đơn vị chính — trượt cả ba tầng giá lẫn bậc
    chiết khấu và trả 0 trong im lặng (review H-2).
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("12000")
        ItemDiscountTierService(session).add(
            item_id=world.item.id, min_quantity=Decimal(10), discount_percent=Decimal(5)
        )

        quoted = world.quote(unit_id=world.base_unit.id, quantity="10")
        assert quoted.source is PriceSource.ITEM_LEVEL
        assert quoted.unit_price == Decimal(12000)
        assert quoted.discount_percent == Decimal(5)


def test_choosing_a_price_list_by_hand_still_checks_that_it_may_apply(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """ "Ép dùng bảng giá này" bỏ qua phép **đi tìm**, không phải phép **kiểm**.

    Bản đầu tiên trả thẳng id, nên một bảng giá đã ngừng theo dõi, hết hạn hay sai
    chiều đều áp được — đúng những ca mà đường tự tìm đã chặn, lọt qua một đường
    khác (review C-2).
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        world = _World(session)
        world.level("1000")

        stopped = world.price_list(is_active=False)
        world.line(stopped, "700")
        assert world.quote(price_list_id=stopped.id).source is PriceSource.ITEM_LEVEL

        expired = world.price_list(effective_to=date(2026, 6, 14))
        world.line(expired, "700")
        assert world.quote(price_list_id=expired.id).source is PriceSource.ITEM_LEVEL
