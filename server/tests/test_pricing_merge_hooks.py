"""Bốn hook hợp nhất của hai bảng giá và bảng bậc chiết khấu (lát 7C-1).

Ba bảng mới của lát này mang khóa duy nhất chứa cột danh mục, nên `merge_records`
đụng chúng ở ba chiều: gộp hai **mã hàng**, gộp hai **đơn vị tính**, gộp hai
**bảng giá**. `test_master_data_merge.py` canh việc hook *tồn tại*; ở đây kiểm nó
làm *đúng việc*.

Đi qua `merge_records` thật chứ không gọi thẳng hook: thứ đáng nghi không chỉ là
luật dọn (đã có bảng giá trị biên ở `test_unit_merge_cleanup.py`) mà là **thứ tự**
— hook phải chạy trước câu `UPDATE` vô danh, và hook từ chối phải đứng trước hook
dọn. Gọi thẳng hook thì cả hai điều ấy không được kiểm.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import unique_code
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.item_discount_tier_service import ItemDiscountTierService
from ket.kernel.master_data.item_price_level_service import ItemPriceLevelService
from ket.kernel.master_data.item_unit_service import ItemUnitService
from ket.kernel.master_data.merge_service import merge_records
from ket.kernel.master_data.models.item import Item, ItemNature
from ket.kernel.master_data.models.item_discount_tier import ItemDiscountTier
from ket.kernel.master_data.models.item_price_level import ItemPriceLevel, PriceDirection
from ket.kernel.master_data.models.price_list import PriceList
from ket.kernel.master_data.models.price_list_line import PriceListLine
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.price_list_line_service import PriceListLineService
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch

pytestmark = pytest.mark.db

SALE = PriceDirection.SALE


def _scope(dataset: DatasetRef, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=branch_ids)


@pytest.fixture
def company_scope(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> RequestScope:
    """Phạm vi **toàn công ty**, đọc lúc chạy chứ không viết cứng.

    Gộp bản ghi là thao tác toàn công ty (`merge_records._ensure_company_wide_scope`,
    H63): người thực hiện phải được gán **mọi** chi nhánh, và mọi câu `UPDATE` của
    lần gộp chạy dưới RLS theo phạm vi ấy. Một phạm vi rỗng thỏa điều kiện đó chỉ
    khi dataset chưa có chi nhánh nào — tức tệp này sẽ xanh khi chạy một mình và
    đỏ ngay khi một tệp test khác tạo chi nhánh trước nó. Đọc lúc chạy là cách duy
    nhất không phụ thuộc thứ tự tệp; cùng lập luận `catalog_api_support.all_branch_codes`.

    Bảng `branches` không mang RLS (miễn trừ tường minh ở `test_rls_policy_coverage`),
    nên đọc nó được từ một phạm vi rỗng.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        return _scope(dataset_alpha, tuple(session.scalars(select(Branch.id)).all()))


def _hooks(slug: str) -> tuple[object, ...]:
    """Hook lấy từ registry, không dựng tay.

    Dựng tay thì bài này xanh trong khi `merge_records` thật chạy một bộ hook
    khác — đúng loại lệch mà `catalog_api_support.catalog_codes` đã tránh cho mã
    quyền.
    """
    spec = REGISTRY.get(slug)
    assert spec is not None
    return spec.merge_hooks


def _merge(session: Session, model: type, slug: str, *, source_id: int, target_id: int) -> None:
    merge_records(
        session,
        model,
        source_id=source_id,
        target_id=target_id,
        actor_branch_ids=frozenset(session.scalars(select(Branch.id)).all()),
        hooks=_hooks(slug),
    )


def _unit(session: Session, name: str) -> UnitOfMeasure:
    units: MasterDataService[UnitOfMeasure] = MasterDataService(session, UnitOfMeasure)
    return units.create(code=unique_code("DV"), name=name)


def _item(session: Session, base_unit_id: int) -> Item:
    items: MasterDataService[Item] = MasterDataService(session, Item)
    return items.create(
        code=unique_code("VT"),
        name="Hàng",
        extra={"nature": ItemNature.GOODS, "base_unit_id": base_unit_id},
    )


def _price_list(session: Session) -> PriceList:
    lists: MasterDataService[PriceList] = MasterDataService(session, PriceList)
    return lists.create(code=unique_code("BG"), name="Bảng giá", extra={"direction": SALE})


def _levels_of(session: Session, item_id: int) -> list[tuple[int | None, int, Decimal]]:
    rows = session.execute(
        select(ItemPriceLevel).where(ItemPriceLevel.item_id == item_id)
    ).scalars()
    return sorted((row.unit_id, row.level, row.price) for row in rows)


def _lines_of(session: Session, price_list_id: int) -> list[tuple[int, int | None, Decimal]]:
    rows = session.execute(
        select(PriceListLine).where(PriceListLine.price_list_id == price_list_id)
    ).scalars()
    return sorted((row.item_id, row.unit_id, row.price) for row in rows)


# ------------------------------------------------------------- gộp hai mã hàng


def test_merging_two_items_keeps_the_price_of_the_record_that_survives(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Hai giá cho cùng một ô: bản ghi được **giữ lại** là bản quyết định.

    Không có hook này thì lần gộp đổ ở `uq_item_price_levels_base_unit` — và hai
    mã hàng đáng gộp thì gần như chắc chắn khai giá cho cùng một ô, vì đó chính
    là lý do chúng bị khai trùng.
    """
    with unit_of_work(session_factory, company_scope) as session:
        unit = _unit(session, "Chiếc")
        source, target = _item(session, unit.id), _item(session, unit.id)
        prices = ItemPriceLevelService(session)
        prices.add(item_id=source.id, unit_id=None, direction=SALE, level=1, price=Decimal(900))
        prices.add(item_id=target.id, unit_id=None, direction=SALE, level=1, price=Decimal(1000))

        _merge(session, Item, "items", source_id=source.id, target_id=target.id)

        assert _levels_of(session, target.id) == [(None, 1, Decimal(1000))]


def test_merging_two_items_carries_over_a_level_the_target_does_not_have(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Ô khác nhau là câu hỏi khác nhau — dòng ấy đi theo bản đích, không bị bỏ."""
    with unit_of_work(session_factory, company_scope) as session:
        unit = _unit(session, "Chiếc")
        source, target = _item(session, unit.id), _item(session, unit.id)
        prices = ItemPriceLevelService(session)
        prices.add(item_id=source.id, unit_id=None, direction=SALE, level=2, price=Decimal(900))
        prices.add(item_id=target.id, unit_id=None, direction=SALE, level=1, price=Decimal(1000))

        _merge(session, Item, "items", source_id=source.id, target_id=target.id)

        assert _levels_of(session, target.id) == [
            (None, 1, Decimal(1000)),
            (None, 2, Decimal(900)),
        ]


def test_merging_two_items_keeps_the_surviving_discount_tier(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Cùng luật cho bậc chiết khấu, khóa là `min_quantity`."""
    with unit_of_work(session_factory, company_scope) as session:
        unit = _unit(session, "Chiếc")
        source, target = _item(session, unit.id), _item(session, unit.id)
        tiers = ItemDiscountTierService(session)
        tiers.add(item_id=source.id, min_quantity=Decimal(10), discount_percent=Decimal(2))
        tiers.add(item_id=source.id, min_quantity=Decimal(20), discount_percent=Decimal(3))
        tiers.add(item_id=target.id, min_quantity=Decimal(10), discount_percent=Decimal(5))

        _merge(session, Item, "items", source_id=source.id, target_id=target.id)

        rows = session.execute(
            select(ItemDiscountTier).where(ItemDiscountTier.item_id == target.id)
        ).scalars()
        assert sorted((row.min_quantity, row.discount_percent) for row in rows) == [
            (Decimal(10), Decimal(5)),
            (Decimal(20), Decimal(3)),
        ]


def test_merging_two_items_keeps_the_surviving_price_list_line(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Chiều thứ ba: cùng một bảng giá khai cả hai mã hàng ở cùng ngưỡng."""
    with unit_of_work(session_factory, company_scope) as session:
        unit = _unit(session, "Chiếc")
        source, target = _item(session, unit.id), _item(session, unit.id)
        price_list = _price_list(session)
        lines = PriceListLineService(session)
        lines.add(
            price_list_id=price_list.id,
            item_id=source.id,
            unit_id=None,
            min_quantity=Decimal(1),
            price=Decimal(900),
        )
        lines.add(
            price_list_id=price_list.id,
            item_id=target.id,
            unit_id=None,
            min_quantity=Decimal(1),
            price=Decimal(1000),
        )

        _merge(session, Item, "items", source_id=source.id, target_id=target.id)

        assert _lines_of(session, price_list.id) == [(target.id, None, Decimal(1000))]


# ---------------------------------------------------------- gộp hai bảng giá


def test_merging_two_price_lists_keeps_the_line_of_the_surviving_list(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Hai bảng giá đáng gộp gần như chắc chắn có mã hàng chung."""
    with unit_of_work(session_factory, company_scope) as session:
        unit = _unit(session, "Chiếc")
        item = _item(session, unit.id)
        source, target = _price_list(session), _price_list(session)
        lines = PriceListLineService(session)
        lines.add(
            price_list_id=source.id,
            item_id=item.id,
            unit_id=None,
            min_quantity=Decimal(1),
            price=Decimal(900),
        )
        lines.add(
            price_list_id=target.id,
            item_id=item.id,
            unit_id=None,
            min_quantity=Decimal(1),
            price=Decimal(1000),
        )

        _merge(session, PriceList, "price_lists", source_id=source.id, target_id=target.id)

        assert _lines_of(session, target.id) == [(item.id, None, Decimal(1000))]


# -------------------------------------------------------- gộp hai đơn vị tính


def test_merging_two_units_collapses_the_two_ways_of_writing_one_price(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Hai đơn vị trùng nghĩa (tỷ lệ 1) khai giá cho cùng một ô: dòng đích thắng."""
    with unit_of_work(session_factory, company_scope) as session:
        base = _unit(session, "Chiếc")
        source_unit, target_unit = _unit(session, "Cái"), _unit(session, "Chiếc lẻ")
        item = _item(session, base.id)
        units = ItemUnitService(session)
        # Tỷ lệ 1 cho cả hai — điều kiện `UnitOfMeasureMergeHook` đòi trước khi
        # nó cho gộp; không có nó thì lần gộp bị từ chối và hook giá không chạy.
        units.add(item_id=item.id, unit_id=source_unit.id, factor=Decimal(1))
        units.add(item_id=item.id, unit_id=target_unit.id, factor=Decimal(1))
        prices = ItemPriceLevelService(session)
        prices.add(
            item_id=item.id, unit_id=source_unit.id, direction=SALE, level=1, price=Decimal(900)
        )
        prices.add(
            item_id=item.id, unit_id=target_unit.id, direction=SALE, level=1, price=Decimal(1000)
        )

        _merge(
            session,
            UnitOfMeasure,
            "units_of_measure",
            source_id=source_unit.id,
            target_id=target_unit.id,
        )

        assert _levels_of(session, item.id) == [(target_unit.id, 1, Decimal(1000))]


def test_a_price_row_becomes_the_base_unit_row_when_the_base_unit_is_merged_away(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Ca đắt nhất: `merge_service` dời cả `items.base_unit_id`.

    Mã hàng lấy đơn vị **nguồn** làm đơn vị chính và khai giá theo đơn vị **đích**.
    Sau lần gộp, đơn vị chính thành đích, nên dòng giá ấy trỏ đúng đơn vị chính —
    trạng thái mà `ensure_unit_is_priceable` cấm ở mọi đường ghi. Nó phải được
    viết lại thành `unit_id NULL`, không phải bị xóa: giá là dữ liệu người dùng
    khai, và ô `NULL` ở đây còn trống.
    """
    with unit_of_work(session_factory, company_scope) as session:
        source_unit, target_unit = _unit(session, "Cái"), _unit(session, "Chiếc")
        item = _item(session, source_unit.id)
        ItemUnitService(session).add(item_id=item.id, unit_id=target_unit.id, factor=Decimal(1))
        ItemPriceLevelService(session).add(
            item_id=item.id, unit_id=target_unit.id, direction=SALE, level=1, price=Decimal(1000)
        )

        _merge(
            session,
            UnitOfMeasure,
            "units_of_measure",
            source_id=source_unit.id,
            target_id=target_unit.id,
        )

        assert _levels_of(session, item.id) == [(None, 1, Decimal(1000))]
        assert session.get(Item, item.id).base_unit_id == target_unit.id


def test_the_price_row_is_dropped_only_when_the_base_unit_slot_is_taken(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Cùng ca trên, nhưng ô `NULL` đã có chủ — lúc đó mới xóa."""
    with unit_of_work(session_factory, company_scope) as session:
        source_unit, target_unit = _unit(session, "Cái"), _unit(session, "Chiếc")
        item = _item(session, source_unit.id)
        ItemUnitService(session).add(item_id=item.id, unit_id=target_unit.id, factor=Decimal(1))
        prices = ItemPriceLevelService(session)
        prices.add(item_id=item.id, unit_id=None, direction=SALE, level=1, price=Decimal(1200))
        prices.add(
            item_id=item.id, unit_id=target_unit.id, direction=SALE, level=1, price=Decimal(1000)
        )

        _merge(
            session,
            UnitOfMeasure,
            "units_of_measure",
            source_id=source_unit.id,
            target_id=target_unit.id,
        )

        assert _levels_of(session, item.id) == [(None, 1, Decimal(1200))]


def test_merging_two_units_collapses_price_list_lines_per_price_list(
    session_factory: sessionmaker[Session], company_scope: RequestScope
) -> None:
    """Phạm vi duy nhất của dòng bảng giá là `(bảng giá, mã hàng)`, không phải mã hàng.

    Cùng một mã hàng nằm trong nhiều bảng giá, và ô `NULL` của bảng giá này không
    nói gì về ô `NULL` của bảng giá kia — gộp chúng làm một sẽ xóa oan dòng hoàn
    toàn hợp lệ.
    """
    with unit_of_work(session_factory, company_scope) as session:
        source_unit, target_unit = _unit(session, "Cái"), _unit(session, "Chiếc")
        item = _item(session, source_unit.id)
        ItemUnitService(session).add(item_id=item.id, unit_id=target_unit.id, factor=Decimal(1))
        first, second = _price_list(session), _price_list(session)
        lines = PriceListLineService(session)
        # Bảng giá thứ nhất đã có dòng theo đơn vị chính; bảng giá thứ hai chưa.
        lines.add(
            price_list_id=first.id,
            item_id=item.id,
            unit_id=None,
            min_quantity=Decimal(1),
            price=Decimal(1200),
        )
        lines.add(
            price_list_id=first.id,
            item_id=item.id,
            unit_id=target_unit.id,
            min_quantity=Decimal(1),
            price=Decimal(1000),
        )
        lines.add(
            price_list_id=second.id,
            item_id=item.id,
            unit_id=target_unit.id,
            min_quantity=Decimal(1),
            price=Decimal(1000),
        )

        _merge(
            session,
            UnitOfMeasure,
            "units_of_measure",
            source_id=source_unit.id,
            target_id=target_unit.id,
        )

        assert _lines_of(session, first.id) == [(item.id, None, Decimal(1200))]
        assert _lines_of(session, second.id) == [(item.id, None, Decimal(1000))]
