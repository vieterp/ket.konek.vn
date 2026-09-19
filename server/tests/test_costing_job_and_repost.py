"""Job `inventory.costing.recalc`, repost giá vốn (ADR-025), chuyển kho chéo khóa
nhiều vòng, cờ `cogs_posted`, xem trước FR-STK-003, hủy, khóa sổ sau khi tính.

Phần tính từng phương pháp nằm ở `test_costing_{wavg_moving,fifo,wavg_period,
specific}.py`; ở đây là những gì bao quanh engine.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from inventory_support import (
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    financial_postings,
    fresh_item,
    issue_payload,
    marks_of_branch,
    movements_of,
    out_movement,
    post_issue,
    post_receipt,
    post_transfer,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    CostingNotConvergingError,
    CostingTouchesLockedPeriodError,
    JobParamsInvalidError,
    PostingValidationError,
)
from ket.kernel.jobs.registry import REGISTRY, JobCancelled, JobContext
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.costing.affected import preview
from ket.modules.inventory.costing.engine import CostingCancelled
from ket.modules.inventory.costing.job import COSTING_JOB, CostingRecalcParams, run_costing_recalc
from ket.modules.inventory.costing.queue import clear_marks, pending_marks
from ket.modules.inventory.lock_check import ensure_inventory_costed
from ket.modules.inventory.models import (
    CostState,
    InventoryBalance,
    InventoryRecalcMark,
    MovementDirection,
)
from ket.modules.inventory.movements import mark_recalc
from ket.modules.inventory.posting_mapper import build_posting_request
from ket.modules.inventory.service import InventoryVoucherService
from ket.modules.inventory.stock import stock_rows
from ket.modules.sales.models import SalesInvoice, SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.contracts import PostingService
from ket.posting.engine.service import REPOST_NOT_POSTED_CODE
from posting_support import USD_RATE, PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
CUSTOMER_ID = 8302
SALESPERSON_ID = 8303
SEP_5, SEP_10, SEP_15, SEP_20 = (
    date(2026, 9, 5),
    date(2026, 9, 10),
    date(2026, 9, 15),
    date(2026, 9, 20),
)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    codes = seed_inventory_package_data(session_factory, dataset_alpha, context)
    codes |= seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-8B-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-8B-01")
    return codes


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


def _job_context(
    session: Session, dataset: DatasetRef, *, branch_id: int | None, progress: FakeProgress
) -> JobContext:
    return JobContext(
        job_id=uuid4(),
        session=session,
        progress=progress,
        attempt=1,
        dataset_schema=dataset.schema_name,
        branch_id=branch_id,
        requested_by=ACTOR_ID,
    )


def _sales(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date,
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    unit_price: Decimal = Decimal(150_000),
) -> SalesInvoiceIn:
    quantity = Decimal(3)
    return SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000889",
        description="bán hàng kiêm xuất kho (8B)",
        is_stock_issue=True,
        lines=(
            SalesInvoiceLineIn(
                description="Hàng B",
                item_id=item_id,
                unit_id=UNIT_PIECE_ID,
                warehouse_id=MAIN_WAREHOUSE_ID,
                quantity=quantity,
                unit_price_fc=unit_price,
                amount_fc=unit_price * quantity,
                vat_rate=Decimal(10),
                vat_amount_fc=(unit_price * quantity) / 10,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                cogs_account_id=accounts["632"],
                inventory_account_id=accounts["156"],
            ),
        ),
    )


def _debits(session: Session, voucher_id: UUID) -> list[tuple[int, Decimal, str]]:
    return [
        (p.account_id, p.debit, p.currency_code)
        for p in financial_postings(session, voucher_id)
        if p.debit > 0
    ]


# ------------------------------------------------------------------- job


def test_job_type_is_registered() -> None:
    assert REGISTRY.get("inventory.costing.recalc") is COSTING_JOB
    assert COSTING_JOB.permission == "inventory.costing.create"


def test_job_runs_per_branch_and_clears_the_marks_it_read(
    run: Runner, context: PostingContext, accounts: dict[str, int], dataset_alpha: DatasetRef
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "JB1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_10, quantity=Decimal(4)
        )
        progress = FakeProgress(reports=[])
        with pytest.raises(JobParamsInvalidError):
            run_costing_recalc(
                _job_context(session, dataset_alpha, branch_id=None, progress=progress),
                CostingRecalcParams(),
            )
        result = run_costing_recalc(
            _job_context(session, dataset_alpha, branch_id=context.branch_id, progress=progress),
            CostingRecalcParams(),
        )
        assert result["branch_id"] == context.branch_id
        assert result["vouchers_reposted"] >= 1
        assert progress.reports[-1][0] == 100
        assert _debits(session, issue) == [(accounts["621"], Decimal(400), "VND")]
        assert not [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]

        # Chạy lại khi không còn gì: điểm bất động ngay vòng đầu, không đổi gì.
        again = run_costing_recalc(
            _job_context(session, dataset_alpha, branch_id=context.branch_id, progress=progress),
            CostingRecalcParams(),
        )
        assert (again["movements_updated"], again["vouchers_reposted"]) == (0, 0)

    run(work)


def test_a_refreshed_mark_survives_the_clear(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Dấu đọc trước, làm mới sau (ghi sổ chen ngang) → `clear_marks` với phiên
    bản cũ không xóa nó."""

    def work(session: Session) -> None:
        item = fresh_item(session, "JB2")
        mark_recalc(
            session,
            branch_id=context.branch_id,
            warehouse_id=MAIN_WAREHOUSE_ID,
            item_id=item,
            lot_id=None,
            from_date=SEP_5,
            reason="test",
        )
        before = pending_marks(session, branch_id=context.branch_id)
        mark_recalc(
            session,
            branch_id=context.branch_id,
            warehouse_id=MAIN_WAREHOUSE_ID,
            item_id=item,
            lot_id=None,
            from_date=SEP_10,
            reason="chen ngang",
        )
        cleared = clear_marks(session, tuple(m for m in before if m.item_id == item))
        assert cleared == 0
        [mark] = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert mark.from_date == SEP_5  # giữ MIN, phiên bản mới
        session.delete(mark)

    run(work)


def test_cancel_rolls_back_at_a_boundary(
    run: Runner, context: PostingContext, accounts: dict[str, int], dataset_alpha: DatasetRef
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "JB3")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_10, quantity=Decimal(4)
        )
        with pytest.raises(CostingCancelled):
            run_engine(session, context, cancel_requested=lambda: True)
        with pytest.raises(JobCancelled):
            run_costing_recalc(
                _job_context(
                    session,
                    dataset_alpha,
                    branch_id=context.branch_id,
                    progress=FakeProgress(reports=[], cancelled=True),
                ),
                CostingRecalcParams(),
            )
        # Ném trước khi chạm gì — trong phiên test vẫn thấy movement chờ giá.
        assert out_movement(session, issue).cost_state == CostState.PENDING

    run(work)


# ------------------------------------------------------- repost & cogs_posted


def test_repost_requires_a_posted_voucher(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "RP1")
        service = InventoryVoucherService(session)
        voucher = service.create(
            issue_payload(
                context, accounts, posting_date=SEP_10, quantity=Decimal(1), item_id=item
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            PostingService(session).repost(
                build_posting_request(session, voucher.id), user_id=ACTOR_ID
            )
        assert caught.value.violations[0].code == REPOST_NOT_POSTED_CODE

    run(work)


def test_stock_issuing_sale_gets_cogs_and_the_flag_even_in_usd(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn USD kiêm xuất kho → phiếu xuất USD; giá vốn ghi bằng VND tỷ giá 1
    → vẫn cân (cân theo từng tiền tệ), `cogs_posted` lật; bỏ ghi sổ hóa đơn →
    cờ hạ, phiếu mất."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RP2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(20_000),
        )
        sales = SalesInvoiceService(session)
        invoice = sales.create(
            _sales(
                context,
                accounts,
                item_id=item,
                posting_date=SEP_10,
                currency_code="USD",
                exchange_rate=USD_RATE,
                unit_price=Decimal(10),
            ),
            user_id=ACTOR_ID,
        )
        sales.post(invoice.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (issue,) = InventoryVoucherService(session).generated_for_source(invoice.id)
        assert issue.currency_code == "USD"
        body = session.get(SalesInvoice, invoice.id)
        assert body is not None and body.cogs_posted is False

        run_engine(session, context)
        assert _debits(session, issue.id) == [(accounts["632"], Decimal(60_000), "VND")]
        credit = [p for p in financial_postings(session, issue.id) if p.credit > 0]
        assert [(p.account_id, p.credit, p.exchange_rate) for p in credit] == [
            (accounts["156"], Decimal(60_000), Decimal(1))
        ]
        session.refresh(body)
        assert body.cogs_posted is True

        sales.unpost(invoice.id, user_id=ACTOR_ID)
        session.refresh(body)
        assert body.cogs_posted is False
        assert InventoryVoucherService(session).generated_for_source(invoice.id) == []

    run(work)


# ---------------------------------------------------------- chuyển kho chéo


def test_transfer_carries_cost_across_keys_in_extra_passes(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """A: R 10@100. CK A→B 4 (gửi bán đại lý, 157/156). B: X 2. Vòng 1: vế đi
    A = 400; vế đến B nhận 400 (STALE). Vòng 2: B tính lại → X 2 = 200. Tổng giá
    trị toàn công ty = 1.000 − 200 (BR-STK-06). CK có bút toán 157/156 = 400."""

    def work(session: Session) -> None:
        item = fresh_item(session, "TR1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        transfer = post_transfer(
            session,
            context,
            item_id=item,
            posting_date=SEP_10,
            quantity=Decimal(4),
            accounts=(accounts["157"], accounts["156"]),
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_15,
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        assert financial_postings(session, transfer) == []

        result = run_engine(session, context)
        assert result.passes >= 3
        legs = {m.direction: m for m in movements_of(session, transfer)}
        assert (legs[MovementDirection.OUT].amount, legs[MovementDirection.IN].amount) == (
            Decimal(400),
            Decimal(400),
        )
        assert legs[MovementDirection.IN].cost_state == CostState.COSTED
        assert out_movement(session, issue).amount == Decimal(200)
        assert _debits(session, transfer) == [(accounts["157"], Decimal(400), "VND")]
        rows = stock_rows(session, as_of=SEP_20, item_id=item)
        assert sum(r.value or 0 for r in rows) == Decimal(800)
        assert {r.warehouse_id: r.on_hand for r in rows} == {
            MAIN_WAREHOUSE_ID: Decimal(6),
            SECOND_WAREHOUSE_ID: Decimal(2),
        }

    run(work)


def test_round_trip_transfer_converges(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """A→B rồi B→A khác ngày: chuỗi hai bước, hội tụ dưới trần."""

    def work(session: Session) -> None:
        item = fresh_item(session, "TR2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_transfer(session, context, item_id=item, posting_date=SEP_10, quantity=Decimal(10))
        post_transfer(
            session,
            context,
            item_id=item,
            posting_date=SEP_15,
            quantity=Decimal(10),
            from_warehouse_id=SECOND_WAREHOUSE_ID,
            to_warehouse_id=MAIN_WAREHOUSE_ID,
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_20, quantity=Decimal(10)
        )
        result = run_engine(session, context)
        assert result.passes <= 5
        assert out_movement(session, issue).amount == Decimal(1000)
        assert sum(r.value or 0 for r in stock_rows(session, as_of=SEP_20, item_id=item)) == 0

    run(work)


# ---------------------------------------------- xem trước, snapshot, khóa sổ


def test_preview_lists_affected_vouchers_and_no_locked_period(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "PV1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_15, quantity=Decimal(4)
        )
        run_engine(session, context)
        backdated = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        seen = preview(session, branch_id=context.branch_id, force_from=None)
        listed = {v.voucher_id: v for v in seen.vouchers}
        assert issue in listed and backdated in listed
        assert listed[issue].document_type == "XK"
        assert seen.locked_periods == ()
        assert seen.keys >= 1 and seen.movements >= 2
        assert seen.earliest_from_date is not None and seen.earliest_from_date <= SEP_10
        assert seen.valuation_method == "wavg_moving"

        forced = preview(session, branch_id=context.branch_id, force_from=SEP_15)
        assert issue in {v.voucher_id for v in forced.vouchers}
        run_engine(session, context)

    run(work)


def test_balances_snapshot_matches_stock_and_period_becomes_lockable(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "SN1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_15, quantity=Decimal(4)
        )
        run_engine(session, context)
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        september = session.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.fiscal_year_id == year.id, AccountingPeriod.period_no == 9
            )
        ).scalar_one()
        snapshot = session.execute(
            select(InventoryBalance).where(
                InventoryBalance.item_id == item, InventoryBalance.period_id == september.id
            )
        ).scalar_one()
        [row] = stock_rows(session, as_of=september.end_date, item_id=item)
        assert (snapshot.closing_qty, snapshot.closing_value) == (row.on_hand, row.value)
        assert (snapshot.in_qty, snapshot.out_qty, snapshot.out_value) == (
            Decimal(10),
            Decimal(4),
            Decimal(400),
        )
        # Mục kiểm khóa sổ 8A (H-2) nay xanh cho kỳ đã tính hết.
        ensure_inventory_costed(session, september, year)

    run(work)


# ------------------------------------------------ nhánh từ chối (review 8B M-6)


def _lock_period(session: Session, period: AccountingPeriod, *, locked: bool) -> None:
    """Đặt/gỡ dấu khóa thẳng trên dòng kỳ — bài kiểm engine, không phải bài
    kiểm `PeriodLockService` (tuần tự + mục kiểm)."""
    period.locked_at = datetime.now(UTC) if locked else None
    period.locked_by = ACTOR_ID if locked else None
    session.flush()


def _period_no(session: Session, context: PostingContext, period_no: int) -> AccountingPeriod:
    return session.execute(
        select(AccountingPeriod).where(
            AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
            AccountingPeriod.period_no == period_no,
        )
    ).scalar_one()


def test_engine_refuses_when_the_horizon_touches_a_locked_period(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Dấu bẩn nằm trong kỳ đã khóa (dựng tay — về cấu trúc không tới được) →
    xem trước nêu kỳ khóa, engine từ chối trước khi ghi."""

    def work(session: Session) -> None:
        item = fresh_item(session, "LK1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=date(2026, 8, 5),
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=date(2026, 8, 10),
            quantity=Decimal(4),
        )
        run_engine(session, context)
        august = _period_no(session, context, 8)
        _lock_period(session, august, locked=True)
        try:
            # Dấu ghi tay vào kỳ đã khóa: `mark_recalc` kẹp dấu mới, nên chèn thẳng.
            session.add(
                InventoryRecalcMark(
                    branch_id=context.branch_id,
                    warehouse_id=MAIN_WAREHOUSE_ID,
                    item_id=item,
                    lot_id=None,
                    lot_key=0,
                    from_date=date(2026, 8, 1),
                    reason="test",
                )
            )
            session.flush()
            seen = preview(session, branch_id=context.branch_id, force_from=None)
            assert [(p.period_no, p.movements) for p in seen.locked_periods] == [(8, 2)]
            with pytest.raises(CostingTouchesLockedPeriodError) as caught:
                run_engine(session, context)
            assert caught.value.details["periods"] == "8"
            assert out_movement(session, issue).amount == Decimal(400)
        finally:
            _lock_period(session, august, locked=False)
            for mark in marks_of_branch(session, context.branch_id):
                if mark.item_id == item:
                    session.delete(mark)

    run(work)


def test_marks_are_clamped_to_the_earliest_open_period(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Nhập bù trỏ về dòng xuất trong kỳ đã khóa → dấu kẹp lên ngày đầu kỳ mở
    sớm nhất (review 8B C-1), job không bị chặn cả chi nhánh."""

    def work(session: Session) -> None:
        item = fresh_item(session, "LK2")
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=date(2026, 8, 10),
            quantity=Decimal(4),
        )
        run_engine(session, context)  # chờ giá — chưa có lớp
        august = _period_no(session, context, 8)
        _lock_period(session, august, locked=True)
        try:
            # Bài kiểm dựng kỳ khóa bằng tay nên dòng chờ giá của tháng 8 vẫn còn;
            # nhập bù ở tháng 9 phải kẹp dấu lên 1/9 chứ không trỏ về 10/8.
            post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=SEP_5,
                quantity=Decimal(10),
                unit_cost=Decimal(100),
            )
            [mark] = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
            assert mark.from_date == date(2026, 9, 1)
        finally:
            _lock_period(session, august, locked=False)
            for mark in marks_of_branch(session, context.branch_id):
                if mark.item_id == item:
                    session.delete(mark)
            # Dòng xuất tháng 8 vẫn chờ (tháng 8 mở lại) — dọn cho bài khác.
            InventoryVoucherService(session).unpost(issue, user_id=ACTOR_ID)

    run(work)


def test_same_day_receipt_after_an_issue_marks_the_key(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cùng ngày: xuất (thứ tự 1) đã tính bằng giá lớp cuối, rồi nhập (thứ tự 2)
    → phải đánh dấu (so bộ `(ngày, thứ tự)`, review 8B H-1)."""

    def work(session: Session) -> None:
        item = fresh_item(session, "SD1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_10, quantity=Decimal(15)
        )
        run_engine(session, context)
        assert not [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_10,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        [mark] = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert mark.from_date == SEP_10

    run(work)


def test_non_convergence_is_refused_at_the_pass_ceiling(
    run: Runner, context: PostingContext, accounts: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ket.modules.inventory.costing import engine as engine_module

    def work(session: Session) -> None:
        item = fresh_item(session, "NC1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_transfer(session, context, item_id=item, posting_date=SEP_10, quantity=Decimal(4))
        post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_15,
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        monkeypatch.setattr(engine_module, "MAX_PASSES", 1)
        with pytest.raises(CostingNotConvergingError) as caught:
            run_engine(session, context)
        assert caught.value.details["passes"] == 1

    run(work)


def test_cancel_after_the_first_pass_leaves_nothing_behind(
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    calls = {"n": 0}

    def cancel_on_second_check() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    def setup(session: Session) -> tuple[int, UUID]:
        item = fresh_item(session, "CX1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        return item, post_issue(
            session, context, accounts, item_id=item, posting_date=SEP_10, quantity=Decimal(4)
        )

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        item, issue = setup(session)
    with pytest.raises(CostingCancelled), unit_of_work(session_factory, scope) as session:
        run_engine(session, context, cancel_requested=cancel_on_second_check)
    assert calls["n"] == 2
    with unit_of_work(session_factory, scope) as session:
        pending = out_movement(session, issue)
        assert (pending.cost_state, pending.amount) == (CostState.PENDING, None)
        assert financial_postings(session, issue) == []
        assert [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item] == []
        run_engine(session, context)


def test_issue_after_only_uncosted_receipts_stays_pending(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Kho đích chỉ có vế đến chuyển kho chưa giá (nguồn chưa từng nhập) → dòng
    xuất ở kho đích CHỜ, không "đã tính" với giá 0 (review 8B M-3)."""

    def work(session: Session) -> None:
        item = fresh_item(session, "UB1")
        post_transfer(session, context, item_id=item, posting_date=SEP_10, quantity=Decimal(4))
        issue = post_issue(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_15,
            quantity=Decimal(2),
            warehouse_id=SECOND_WAREHOUSE_ID,
        )
        result = run_engine(session, context)
        moved = out_movement(session, issue)
        assert (moved.cost_state, moved.unit_cost) == (CostState.PENDING, None)
        assert result.pending_left >= 2

    run(work)


def test_cogs_flag_is_lowered_when_the_only_layer_is_unposted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "CF1")
        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=SEP_5,
            quantity=Decimal(10),
            unit_cost=Decimal(20_000),
        )
        sales = SalesInvoiceService(session)
        invoice = sales.create(
            _sales(context, accounts, item_id=item, posting_date=SEP_10), user_id=ACTOR_ID
        )
        sales.post(invoice.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        run_engine(session, context)
        body = session.get(SalesInvoice, invoice.id)
        assert body is not None and body.cogs_posted is True
        (issue,) = InventoryVoucherService(session).generated_for_source(invoice.id)
        assert financial_postings(session, issue.id) != []

        # Gỡ lớp duy nhất: dòng xuất về chờ, bút toán 632 bị rút, cờ hạ.
        InventoryVoucherService(session).unpost(receipt, user_id=ACTOR_ID)
        run_engine(session, context)
        session.refresh(body)
        assert body.cogs_posted is False
        assert financial_postings(session, issue.id) == []

    run(work)
