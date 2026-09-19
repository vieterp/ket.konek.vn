"""Đích danh (lát 8B): dòng xuất chỉ lần nhập (`source_movement_id`, migration
0046) — service kiểm lúc cất, engine lấy giá của đúng lần nhập, lớp tồn theo
lần nhập. Năm test đổi sang `specific` và trả lại `wavg_moving` ở `finally`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    SECOND_WAREHOUSE_ID,
    financial_postings,
    fresh_item,
    in_movement,
    out_movement,
    post_issue,
    post_receipt,
    receipt_payload,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import InventoryLayerReferencedError, PostingValidationError
from ket.kernel.periods.models import InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.models import CostState, StockLayer
from ket.modules.inventory.service import (
    SPECIFIC_SOURCE_MISMATCH_CODE,
    SPECIFIC_SOURCE_ON_RECEIPT_CODE,
    SPECIFIC_SOURCE_REQUIRED_CODE,
    InventoryVoucherService,
)
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5, MAR_10, MAR_15 = date(2026, 3, 5), date(2026, 3, 10), date(2026, 3, 15)


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
def _specific_year(run: Runner, context: PostingContext) -> None:  # type: ignore[misc]
    def switch(method: str) -> Callable[[Session], None]:
        def work(session: Session) -> None:
            set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none")
            set_valuation_method(session, context, method)

        return work

    run(switch(InventoryValuationMethod.SPECIFIC.value))
    try:
        yield
    finally:
        run(switch(InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value))


def test_issue_lines_must_name_a_receipt_of_the_same_key(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "SP1")
        other_item = fresh_item(session, "SP1b")
        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        other = post_receipt(
            session,
            context,
            accounts,
            item_id=other_item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(999),
        )
        with pytest.raises(PostingValidationError) as missing:
            post_issue(
                session, context, accounts, item_id=item, posting_date=MAR_10, quantity=Decimal(4)
            )
        assert missing.value.violations[0].code == SPECIFIC_SOURCE_REQUIRED_CODE
        with pytest.raises(PostingValidationError) as mismatch:
            post_issue(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=MAR_10,
                quantity=Decimal(4),
                source_movement_id=in_movement(session, other).id,
            )
        assert mismatch.value.violations[0].code == SPECIFIC_SOURCE_MISMATCH_CODE
        with pytest.raises(PostingValidationError) as wrong_warehouse:
            post_issue(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=MAR_10,
                quantity=Decimal(4),
                warehouse_id=SECOND_WAREHOUSE_ID,
                source_movement_id=in_movement(session, receipt).id,
            )
        assert wrong_warehouse.value.violations[0].code == SPECIFIC_SOURCE_MISMATCH_CODE
        # Phiếu nhập không chỉ lần nhập nguồn.
        payload = receipt_payload(context, accounts, posting_date=MAR_10, item_id=item)
        payload = payload.model_copy(
            update={
                "lines": tuple(
                    line.model_copy(update={"source_movement_id": in_movement(session, receipt).id})
                    for line in payload.lines
                )
            }
        )
        with pytest.raises(PostingValidationError) as on_receipt:
            InventoryVoucherService(session).create(payload, user_id=ACTOR_ID)
        assert on_receipt.value.violations[0].code == SPECIFIC_SOURCE_ON_RECEIPT_CODE

    run(work)


def test_issue_takes_the_named_receipt_price_and_layers_follow(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hai lớp 100 và 300; xuất 4 chỉ lớp 300 → 1.200 dù lớp 100 nhập trước;
    lớp 300 còn 6, lớp 100 nguyên 10."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SP2")
        cheap = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        dear = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        dear_movement = in_movement(session, dear)
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_15,
            quantity=Decimal(4),
            source_movement_id=dear_movement.id,
        )
        assert out_movement(session, issue).source_movement_id == dear_movement.id

        run_engine(session, context)
        moved = out_movement(session, issue)
        assert (moved.unit_cost, moved.amount, moved.cost_state) == (
            Decimal("300.000000"),
            Decimal(1200),
            CostState.COSTED,
        )
        assert [p.debit for p in financial_postings(session, issue) if p.debit > 0] == [
            Decimal(1200)
        ]
        layers = {
            layer.movement_id: layer.remaining_qty
            for layer in session.execute(
                select(StockLayer).where(StockLayer.item_id == item)
            ).scalars()
        }
        assert layers == {
            in_movement(session, cheap).id: Decimal(10),
            dear_movement.id: Decimal(6),
        }

    run(work)


def test_a_receipt_named_by_a_specific_issue_cannot_be_unposted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "SP3")
        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(4),
            source_movement_id=in_movement(session, receipt).id,
        )
        service = InventoryVoucherService(session)
        with pytest.raises(InventoryLayerReferencedError) as caught:
            service.unpost(receipt, user_id=ACTOR_ID)
        assert caught.value.details["count"] == 1
        # Gỡ phiếu xuất trước thì lần nhập bỏ ghi sổ được.
        service.unpost(issue, user_id=ACTOR_ID)
        service.delete(issue)
        service.unpost(receipt, user_id=ACTOR_ID)

    run(work)
