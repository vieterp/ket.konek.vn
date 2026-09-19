"""Bình quân gia quyền cuối kỳ (lát 8B): một giá cho cả kỳ, lăn giữa các kỳ,
mẫu 0 → bình quân kỳ trước; horizon ghi là trọn kỳ."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    fresh_item,
    out_movement,
    post_issue,
    post_receipt,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.models import CostState
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5, MAR_20, MAR_25 = date(2026, 3, 5), date(2026, 3, 20), date(2026, 3, 25)
APR_10, MAY_10, JUN_10 = date(2026, 4, 10), date(2026, 5, 10), date(2026, 6, 10)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    return seed_inventory_package_data(session_factory, dataset_alpha, context)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


@pytest.fixture(autouse=True)
def _period_year(run: Runner, context: PostingContext) -> None:  # type: ignore[misc]
    def switch(method: str) -> Callable[[Session], None]:
        def work(session: Session) -> None:
            set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none")
            set_valuation_method(session, context, method)

        return work

    run(switch(InventoryValuationMethod.WEIGHTED_AVERAGE_PERIOD.value))
    try:
        yield
    finally:
        run(switch(InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value))


def test_one_price_per_period_rolling_into_the_next(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """T3: R 10@100 (5/3), X 5 (20/3), R 10@300 (25/3) → giá T3 = 4.000/20 = 200
    kể cả cho X trước lần nhập sau → 1.000. T4: X 10, tồn đầu 15 = 3.000 → 200 →
    2.000. T5: X 5 → 1.000, còn 0. T6: X 3 khi mẫu 0 → bình quân kỳ trước 200."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WP1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        march = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_20, quantity=Decimal(5)
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_25,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        april = post_issue(
            session, context, accounts, item_id=item, posting_date=APR_10, quantity=Decimal(10)
        )
        may = post_issue(
            session, context, accounts, item_id=item, posting_date=MAY_10, quantity=Decimal(5)
        )
        june = post_issue(
            session, context, accounts, item_id=item, posting_date=JUN_10, quantity=Decimal(3)
        )
        result = run_engine(session, context)
        assert result.passes >= 2
        for voucher, amount in ((march, 1000), (april, 2000), (may, 1000), (june, 600)):
            moved = out_movement(session, voucher)
            assert (moved.unit_cost, moved.amount, moved.cost_state) == (
                Decimal("200.000000"),
                Decimal(amount),
                CostState.COSTED,
            ), voucher

    run(work)


def test_a_late_receipt_in_a_period_rewrites_the_whole_period(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Đã tính T3 = 100. Nhập thêm 10@300 ngày 25/3 (sau mọi phiếu xuất, không
    phải lùi ngày) → giá T3 đổi thành 200 cho cả phiếu xuất 20/3."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WP2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_20, quantity=Decimal(5)
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(500)
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_25,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(1000)

    run(work)
