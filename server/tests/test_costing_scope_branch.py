"""Phạm vi tính bình quân "không theo kho" (FR-STK-007, lát 8C-1).

`fiscal_years.inventory_costing_by_warehouse = false` → hai câu bình quân gom mọi
kho của (chi nhánh, mã hàng, lô) thành một khóa giá; movement vẫn mang kho
riêng. Bất biến kiểm ở đây:

* BQ tức thời: xuất ở kho B nhận bình quân của cả A lẫn B theo thứ tự thời gian
  của cả nhóm (xuất trước khi B có nhập → giá của A);
* BQ cuối kỳ: mọi dòng xuất trong kỳ nhận giá kỳ của cả nhóm;
* chuyển kho nội bộ trong nhóm hội tụ qua vòng, tổng giá trị nhóm không đổi;
* FIFO bỏ qua cờ (độc lập từng kho — SRS 09 §3);
* dấu bẩn ở một kho kéo cả nhóm tính lại.

Năm tài chính dùng chung cả phiên nên mỗi bài trả cờ về `true` ở `finally`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    fresh_item,
    in_movement,
    marks_of_branch,
    movements_of,
    out_movement,
    post_issue,
    post_receipt,
    post_transfer,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import FiscalYear, InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.costing.affected import preview
from ket.modules.inventory.costing.engine import BRANCH_SCOPE_SQL, METHOD_SQL, method_sql_for
from ket.modules.inventory.models import InventoryMovement, MovementDirection
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_opening_balances_carry_forward import ensure_fiscal_year

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_5, JAN_8, JAN_10, JAN_15, JAN_20 = (
    date(2026, 1, 5),
    date(2026, 1, 8),
    date(2026, 1, 10),
    date(2026, 1, 15),
    date(2026, 1, 20),
)
FEB_3 = date(2026, 2, 3)


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
def _guard_off(run: Runner) -> None:
    run(lambda session: set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none"))


@pytest.fixture
def branch_scope(run: Runner, context: PostingContext) -> None:
    """Cờ "không theo kho" cho bài kiểm, trả lại "theo kho" sau đó."""

    def set_flag(value: bool) -> Callable[[Session], None]:
        def work(session: Session) -> None:
            year = session.get(FiscalYear, context.fiscal_year_id)
            assert year is not None
            year.inventory_costing_by_warehouse = value
            session.flush()

        return work

    run(set_flag(False))
    yield
    run(set_flag(True))


def _method(run: Runner, context: PostingContext, method: str) -> None:
    run(lambda session: set_valuation_method(session, context, method))


def test_engine_picks_the_branch_scope_sql_only_for_averages() -> None:
    for method in (
        InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
        InventoryValuationMethod.WEIGHTED_AVERAGE_PERIOD,
    ):
        scoped = FiscalYear(inventory_valuation_method=method, inventory_costing_by_warehouse=False)
        assert method_sql_for(scoped) is BRANCH_SCOPE_SQL[method]
        per_warehouse = FiscalYear(
            inventory_valuation_method=method, inventory_costing_by_warehouse=True
        )
        assert method_sql_for(per_warehouse) is METHOD_SQL[method]
    for method in (InventoryValuationMethod.FIFO, InventoryValuationMethod.SPECIFIC):
        scoped = FiscalYear(inventory_valuation_method=method, inventory_costing_by_warehouse=False)
        assert method_sql_for(scoped) is METHOD_SQL[method]


@pytest.mark.usefixtures("branch_scope")
def test_moving_average_spans_warehouses_in_time_order(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """A: R 5/1 10@100. B: X 8/1 2 (trước khi B có nhập) → giá nhóm = 100 → 200.
    B: R 10/1 10@300. B: X 15/1 5 → bình quân nhóm (800 + 3.000)/18 = 211,111111
    → 1.055,56. Theo kho thì X 8/1 chờ giá và X 15/1 = 1.500."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SC1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        early = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_8,
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        late = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_15,
            quantity=Decimal(5),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        result = run_engine(session, context)
        assert result.pending_left == 0
        first, second = out_movement(session, early), out_movement(session, late)
        assert (first.unit_cost, first.amount) == (Decimal("100.000000"), Decimal("200.00"))
        assert (second.unit_cost, second.amount) == (Decimal("211.111111"), Decimal("1055.56"))

    run(work)


@pytest.mark.usefixtures("branch_scope")
def test_period_average_spans_warehouses(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cùng dữ liệu bài trên, BQ cuối kỳ: cả hai dòng xuất tháng 1 nhận giá kỳ
    của nhóm = (1.000 + 3.000)/20 = 200 — kể cả dòng xuất đứng trước lần nhập ở B."""
    _method(run, context, InventoryValuationMethod.WEIGHTED_AVERAGE_PERIOD)
    try:

        def work(session: Session) -> None:
            item = fresh_item(session, "SC2")
            post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_5,
                quantity=Decimal(10),
                unit_cost=Decimal(100),
            )
            early = post_issue(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_8,
                quantity=Decimal(2),
                warehouse_id=SECOND_WAREHOUSE_ID,
            )
            post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_10,
                quantity=Decimal(10),
                unit_cost=Decimal(300),
                warehouse_id=SECOND_WAREHOUSE_ID,
            )
            late = post_issue(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_15,
                quantity=Decimal(5),
                warehouse_id=SECOND_WAREHOUSE_ID,
            )
            run_engine(session, context)
            assert out_movement(session, early).amount == Decimal("400.00")
            assert out_movement(session, late).amount == Decimal("1000.00")

        run(work)
    finally:
        _method(run, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING)


@pytest.mark.usefixtures("branch_scope")
def test_internal_transfer_converges_and_keeps_group_value(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """A: R 10@100, R 10@300 → nhóm 20 = 4.000. CK A→B 4 → vế đi 800, vế đến 800
    (STALE rồi COSTED). B: X 2 → 400. Tổng giá trị nhóm = 4.000 − 400."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SC3")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_8,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        transfer = post_transfer(
            session, context, item_id=item, posting_date=JAN_10, quantity=Decimal(4)
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_15,
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        result = run_engine(session, context)
        assert result.pending_left == 0 and result.passes <= 3
        legs = movements_of(session, transfer)
        assert {(m.direction, m.amount) for m in legs} == {
            (MovementDirection.OUT, Decimal("800.00")),
            (MovementDirection.IN, Decimal("800.00")),
        }
        assert out_movement(session, issue).amount == Decimal("400.00")
        group_value = session.scalar(
            select(func.sum(InventoryMovement.direction * InventoryMovement.amount)).where(
                InventoryMovement.item_id == item
            )
        )
        assert group_value == Decimal("3600.00")

    run(work)


@pytest.mark.usefixtures("branch_scope")
def test_fifo_ignores_the_branch_scope_flag(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """A: R 10@100; B: R 10@300; B: X 5 → FIFO độc lập từng kho = 1.500 (không phải
    lớp 100 của A)."""
    _method(run, context, InventoryValuationMethod.FIFO)
    try:

        def work(session: Session) -> None:
            item = fresh_item(session, "SC4")
            post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_5,
                quantity=Decimal(10),
                unit_cost=Decimal(100),
            )
            post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_10,
                quantity=Decimal(10),
                unit_cost=Decimal(300),
                warehouse_id=SECOND_WAREHOUSE_ID,
            )
            issue = post_issue(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=JAN_15,
                quantity=Decimal(5),
                warehouse_id=SECOND_WAREHOUSE_ID,
            )
            run_engine(session, context)
            assert out_movement(session, issue).amount == Decimal("1500.00")

        run(work)
    finally:
        _method(run, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING)


@pytest.mark.usefixtures("branch_scope")
def test_a_backdated_receipt_in_one_warehouse_reprices_the_whole_group(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """B: R 10@300, X 15/1 5 → 1.500. Sau đó A: R lùi ngày 5/1 10@100 (dấu bẩn ở
    khóa A) → lượt sau tính lại cả nhóm: X ở B = 5 × 200 = 1.000."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SC5")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_15,
            quantity=Decimal(5),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal("1500.00")
        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        assert in_movement(session, receipt).warehouse_id == MAIN_WAREHOUSE_ID
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal("1000.00")

    run(work)


@pytest.mark.usefixtures("branch_scope")
def test_next_year_mark_reaches_a_warehouse_without_its_own_mark(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review H-4: B: R 10@300 (2026), X 5 (2026) và X 2 (1/2027) — tính xong. A: R
    lùi ngày 12/2026 10@100 (dấu chỉ ở A, A không có movement 2027) → lượt 2026
    tính lại nhóm và phải để dấu 2027 cho **kho B** → lượt sau X 2027 = 2 × 200."""

    def work(session: Session) -> None:
        # Năm 2027 dùng chung cả phiên: tắt cờ cho bài này rồi trả lại ở `finally`.
        next_year = session.get(FiscalYear, ensure_fiscal_year(session, "2027", date(2027, 1, 1)))
        assert next_year is not None
        next_year.inventory_costing_by_warehouse = False
        session.flush()
        try:
            _work(session, next_year)
        finally:
            next_year.inventory_costing_by_warehouse = True
            session.flush()

    def _work(session: Session, next_year: FiscalYear) -> None:
        item = fresh_item(session, "SC6")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_15,
            quantity=Decimal(5),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        later = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=date(2027, 1, 8),
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        run_engine(session, context)
        assert out_movement(session, later).amount == Decimal("600.00")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=date(2026, 12, 20),
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        first = run_engine(session, context)
        assert "2026" in first.years
        marks = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert [(m.warehouse_id, m.from_date) for m in marks] == [
            (SECOND_WAREHOUSE_ID, date(2027, 1, 1))
        ]
        second = run_engine(session, context)
        assert second.years == ["2027"]
        # Nhóm cuối 2026: B 5@300 còn 1.500 + A 10@100 = 2.500 / 15 = 166,666667.
        assert out_movement(session, later).amount == Decimal("333.33")

    run(work)


@pytest.mark.usefixtures("branch_scope")
def test_preview_counts_the_whole_group_from_one_warehouse_mark(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review M-1: dấu ở kho A phải kéo movement kho B vào xem trước / kiểm kỳ khóa."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SC7")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_15,
            quantity=Decimal(5),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        run_engine(session, context)
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=JAN_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        seen = preview(session, branch_id=context.branch_id, force_from=None)
        assert issue in {row.voucher_id for row in seen.vouchers}

    run(work)
