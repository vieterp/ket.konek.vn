"""FIFO (lát 8B) — đối chiếu tay, lớp tồn `stock_layers`, nhập bù, gỡ lớp.

Bài đổi phương pháp của năm test sang `fifo` và trả lại `wavg_moving` trong
`finally` (dataset dùng chung cả phiên). Luật vượt tồn: xuất khi chưa đủ lớp
"ăn" lớp nhập sau; hết cả lớp sau → giá lớp cuối; không lớp → chờ.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    financial_postings,
    fresh_item,
    in_movement,
    marks_of_branch,
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
from ket.modules.inventory.models import CostState, StockLayer
from ket.modules.inventory.service import InventoryVoucherService
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5 = date(2026, 3, 5)
MAR_10 = date(2026, 3, 10)
MAR_15 = date(2026, 3, 15)
MAR_20 = date(2026, 3, 20)


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
def _fifo_year(run: Runner, context: PostingContext) -> None:  # type: ignore[misc]
    def switch(method: str) -> Callable[[Session], None]:
        def work(session: Session) -> None:
            set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none")
            set_valuation_method(session, context, method)

        return work

    run(switch(InventoryValuationMethod.FIFO.value))
    try:
        yield
    finally:
        run(switch(InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value))


def _layers(session: Session, item_id: int) -> list[StockLayer]:
    return list(
        session.execute(
            select(StockLayer)
            .where(StockLayer.item_id == item_id)
            .order_by(StockLayer.receipt_date, StockLayer.sequence_in_day)
        )
        .scalars()
        .all()
    )


def _cogs(session: Session, voucher_id: UUID) -> tuple[Decimal, ...]:
    return tuple(p.debit for p in financial_postings(session, voucher_id) if p.debit > 0)


def test_issue_consumes_layers_in_receipt_order(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 10@100, R 10@200, X 15 → 10×100 + 5×200 = 2.000; lớp 1 hết, lớp 2 còn 5.
    X 10 nữa → 5×200 + đuôi 5 × giá lớp cuối 200 = 2.000; lớp 2 về 0."""

    def work(session: Session) -> None:
        item = fresh_item(session, "FI1")
        first = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        second = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(200),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_15, quantity=Decimal(15)
        )
        run_engine(session, context)
        moved = out_movement(session, issue)
        assert (moved.amount, moved.unit_cost) == (Decimal(2000), Decimal("133.333333"))
        assert _cogs(session, issue) == (Decimal(2000),)
        layers = _layers(session, item)
        assert [(layer.movement_id, layer.remaining_qty) for layer in layers] == [
            (in_movement(session, first).id, Decimal(0)),
            (in_movement(session, second).id, Decimal(5)),
        ]

        tail = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_20, quantity=Decimal(10)
        )
        run_engine(session, context)
        assert out_movement(session, tail).amount == Decimal(2000)
        assert [layer.remaining_qty for layer in _layers(session, item)] == [Decimal(0), Decimal(0)]

    run(work)


def test_issue_before_any_layer_waits_then_eats_the_later_receipt(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """X 5 (5/3) khi chưa có lớp → chờ giá. R 10@100 (10/3) → dấu "nhập bù" từ
    5/3 → X 5 lấy 500 từ lớp nhập sau. Bỏ ghi sổ lần nhập → lớp mất → X 5 về chờ
    giá và bút toán giá vốn bị gỡ."""

    def work(session: Session) -> None:
        item = fresh_item(session, "FI2")
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_5, quantity=Decimal(5)
        )
        run_engine(session, context)
        first = out_movement(session, issue)
        assert (first.cost_state, first.unit_cost, first.amount) == (CostState.PENDING, None, None)

        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        [mark] = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert mark.from_date == MAR_5
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(500)
        assert _cogs(session, issue) == (Decimal(500),)
        assert [layer.remaining_qty for layer in _layers(session, item)] == [Decimal(5)]

        InventoryVoucherService(session).unpost(receipt, user_id=ACTOR_ID)
        run_engine(session, context)
        pending = out_movement(session, issue)
        assert (pending.cost_state, pending.amount) == (CostState.PENDING, None)
        assert financial_postings(session, issue) == []
        assert _layers(session, item) == []

    run(work)


def test_backdated_receipt_reshuffles_layers(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 10@200 (10/3), X 5 (15/3) → 1.000. Chèn R 10@100 lùi 5/3 → lớp rẻ đứng
    trước → X 5 = 500 (FR-STK-003)."""

    def work(session: Session) -> None:
        item = fresh_item(session, "FI3")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(200),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_15, quantity=Decimal(5)
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(1000)
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(500)
        assert _cogs(session, issue) == (Decimal(500),)
        assert [layer.remaining_qty for layer in _layers(session, item)] == [
            Decimal(5),
            Decimal(10),
        ]

    run(work)
