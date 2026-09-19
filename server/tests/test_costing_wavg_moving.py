"""Bình quân gia quyền tức thời (lát 8B) — đối chiếu tay từng kịch bản.

Mỗi bài dùng một mã hàng MỚI (khóa tồn kho sạch — dataset dùng chung cả phiên),
chạy engine thẳng trong phiên (`run_engine`), rồi so `unit_cost`/`amount` của
movement xuất, bút toán giá vốn được ghi lại (`PostingService.repost`,
ADR-025) và dấu bẩn với con số tính tay. Luật vượt tồn theo quyết định user
2026-09-19 (xem docstring `sql/wavg_moving.sql`).
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
    MAIN_WAREHOUSE_ID,
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
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.models import CostState, InventoryVoucherKind
from ket.modules.inventory.movements import reorder_day
from ket.modules.inventory.schemas import ReorderDayIn
from ket.modules.inventory.service import InventoryVoucherService
from ket.modules.inventory.stock import stock_rows
from ket.posting.balances.models import BalanceRecalcQueue
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5 = date(2026, 3, 5)
MAR_10 = date(2026, 3, 10)
MAR_12 = date(2026, 3, 12)
MAR_15 = date(2026, 3, 15)
MAR_20 = date(2026, 3, 20)
MAR_25 = date(2026, 3, 25)


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


def _cogs_amounts(session: Session, voucher_id: UUID) -> tuple[Decimal, ...]:
    """Thành tiền dòng Nợ của bút toán giá vốn (sổ tài chính)."""
    return tuple(p.debit for p in financial_postings(session, voucher_id) if p.debit > 0)


def test_interleaved_receipts_and_issues_match_hand_computation(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 10@100, R 10@200, X 5 → 150; R 10@300 → tồn 25 = 5.250 (bình quân 210);
    X 25 xuất hết → lấy trọn 5.250 (không rơi lẻ)."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WM1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(200),
        )
        issue_a = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_12, quantity=Decimal(5)
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_15,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        issue_b = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_20, quantity=Decimal(25)
        )
        assert financial_postings(session, issue_a) == []

        result = run_engine(session, context)
        assert result.passes >= 2  # vòng cuối là vòng xác nhận không đổi

        first, second = out_movement(session, issue_a), out_movement(session, issue_b)
        assert (first.unit_cost, first.amount, first.cost_state) == (
            Decimal("150.000000"),
            Decimal(750),
            CostState.COSTED,
        )
        assert (second.unit_cost, second.amount) == (Decimal("210.000000"), Decimal(5250))
        assert _cogs_amounts(session, issue_a) == (Decimal(750),)
        assert _cogs_amounts(session, issue_b) == (Decimal(5250),)
        # Sổ cái ghi lại → số dư đánh dấu bẩn (cùng luật `post`).
        assert (
            session.scalar(
                select(BalanceRecalcQueue.marked_at).where(
                    BalanceRecalcQueue.branch_id == context.branch_id
                )
            )
            is not None
        )
        [row] = stock_rows(session, as_of=MAR_25, item_id=item)
        assert (row.on_hand, row.value) == (Decimal(0), Decimal(0))

    run(work)


def test_over_issue_uses_current_then_last_average_and_no_basis_stays_pending(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 10@100; X 15 → 15 × 100 = 1.500 (tồn về −5, giá trị −500); X 5 khi tồn
    ≤ 0 → bình quân gần nhất 100 = 500. Khóa chưa từng nhập → chờ giá."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WM2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        over = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_10, quantity=Decimal(15)
        )
        after = post_issue(
            session, context, accounts, item_id=item, posting_date=MAR_15, quantity=Decimal(5)
        )
        orphan_item = fresh_item(session, "WM2b")
        orphan = post_issue(
            session,
            context,
            accounts,
            item_id=orphan_item,
            posting_date=MAR_10,
            quantity=Decimal(3),
        )

        result = run_engine(session, context)

        assert out_movement(session, over).amount == Decimal(1500)
        assert out_movement(session, after).amount == Decimal(500)
        pending = out_movement(session, orphan)
        assert (pending.cost_state, pending.unit_cost) == (CostState.PENDING, None)
        assert financial_postings(session, orphan) == []
        assert result.pending_left >= 1

    run(work)


def test_backdated_receipt_recomputes_later_issues_and_reposts_cogs(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 10@100 (5/3), X 5 (20/3) → 500. Chèn R 10@300 lùi ngày 10/3 → dấu bẩn
    từ 10/3 → X 5 tính lại theo bình quân 200 = 1.000, bút toán cũ 500 biến mất."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WM3")
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
        assert _cogs_amounts(session, issue) == (Decimal(500),)
        assert not [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]

        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        [mark] = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert mark.from_date == MAR_10

        run_engine(session, context)
        moved = out_movement(session, issue)
        assert (moved.unit_cost, moved.amount) == (Decimal("200.000000"), Decimal(1000))
        assert _cogs_amounts(session, issue) == (Decimal(1000),)
        assert not [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]

    run(work)


def test_reordering_a_day_changes_the_issue_price(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cùng ngày 10/3: X 5 (thứ tự 1) rồi R 10@300 (thứ tự 2) trên tồn 10@100 →
    500. Đổi thứ tự [R, X] → bình quân 200 → 1.000 (FR-STK-017, BR-STK-04)."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WM4")
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
            session, context, accounts, item_id=item, posting_date=MAR_10, quantity=Decimal(5)
        )
        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal(500)

        issue_movement = out_movement(session, issue)
        receipt_movement = in_movement(session, receipt)
        reorder_day(
            session,
            ReorderDayIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                item_id=item,
                posting_date=MAR_10,
                ordered_movement_ids=(receipt_movement.id, issue_movement.id),
            ),
        )
        result = run_engine(session, context)
        moved = out_movement(session, issue)
        assert (moved.unit_cost, moved.amount) == (Decimal("200.000000"), Decimal(1000))
        assert _cogs_amounts(session, issue) == (Decimal(1000),)
        # Dòng nhập bị đánh STALE bởi lượt sắp xếp → về COSTED, giá không đổi.
        session.refresh(receipt_movement)
        assert receipt_movement.cost_state == CostState.COSTED
        assert result.movements_updated >= 1

    run(work)


def test_unpost_and_repost_of_an_issue_is_stable(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bỏ ghi sổ phiếu xuất đã có giá vốn → GL mất, movement mất, dấu bẩn; ghi
    sổ lại → 0 GL, movement chờ giá; engine → cùng con số cũ."""

    def work(session: Session) -> None:
        item = fresh_item(session, "WM5")
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
            session, context, accounts, item_id=item, posting_date=MAR_10, quantity=Decimal(4)
        )
        run_engine(session, context)
        assert _cogs_amounts(session, issue) == (Decimal(400),)

        service = InventoryVoucherService(session)
        service.unpost(issue, user_id=ACTOR_ID)
        assert financial_postings(session, issue) == []
        service.post(issue, user_id=ACTOR_ID, acknowledged_warnings=True)
        assert financial_postings(session, issue) == []
        assert out_movement(session, issue).cost_state == CostState.PENDING

        run_engine(session, context)
        assert _cogs_amounts(session, issue) == (Decimal(400),)
        body = service.get(issue)[1]
        assert body.kind == InventoryVoucherKind.ISSUE

    run(work)
