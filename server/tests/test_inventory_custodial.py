"""Hàng nhận giữ hộ / bán hộ (BR-STK-07, lát 8D) trên PostgreSQL thật.

Bất biến của lát: dòng `is_custodial` theo dõi **số lượng** và không đi vào giá
trị tồn, sổ cái, engine tính giá, hay mục kiểm khóa sổ.

* Phiếu toàn dòng giữ hộ ghi sổ được với **0 dòng GL** (đường ADR-024), movement
  mang `cost_state = NOT_APPLICABLE`, không dấu bẩn nào sinh ra.
* Khóa tồn kho vừa có hàng của mình vừa có hàng giữ hộ: cả **bốn** phương pháp
  tính giá bỏ qua phần giữ hộ — đây là chỗ lọc `is_custodial` trong 20 tệp SQL
  của 8B/8C lần đầu có dữ liệu thật đi qua.
* Khóa sổ kỳ được dù còn dòng giữ hộ chưa (và không bao giờ) có giá.
* Guard tồn âm soi **riêng từng loại**: xuất giữ hộ quá số nhận thì kêu, và nó
  không được bù bằng hàng cùng mã của đơn vị.
* Dòng giữ hộ có cặp TK / có giá / trên phiếu lắp ráp → từ chối.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    custodial_issue_payload,
    custodial_receipt_payload,
    financial_postings,
    fresh_item,
    marks_of_branch,
    movements_of,
    post_custodial_receipt,
    post_issue,
    post_receipt,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PeriodLockChecklistError, PostingValidationError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.guards import STOCK_NEGATIVE_CODE
from ket.modules.inventory.lock_check import ensure_inventory_costed
from ket.modules.inventory.models import CostState, InventoryVoucherKind, MovementDirection
from ket.modules.inventory.schemas import InventoryVoucherIn, InventoryVoucherLineIn
from ket.modules.inventory.service import (
    CUSTODIAL_KIND_NOT_ALLOWED_CODE,
    CUSTODIAL_LINE_HAS_ACCOUNTS_CODE,
    InventoryVoucherService,
)
from ket.modules.inventory.stock import stock_rows
from ket.posting.contracts import VoucherStatus
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
APR_5 = date(2026, 4, 5)
APR_10 = date(2026, 4, 10)
APR_15 = date(2026, 4, 15)


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


def test_custodial_receipt_posts_without_any_ledger_line(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-plain")
        service = InventoryVoucherService(session)
        voucher = service.create(
            custodial_receipt_payload(
                context, posting_date=APR_5, quantity=Decimal(40), item_id=item_id
            ),
            user_id=ACTOR_ID,
        )
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert posted.status == VoucherStatus.DA_GHI_SO
        assert financial_postings(session, voucher.id) == []

        movements = movements_of(session, voucher.id)
        assert len(movements) == 1
        assert movements[0].is_custodial is True
        assert movements[0].cost_state == CostState.NOT_APPLICABLE
        assert movements[0].unit_cost is None
        assert movements[0].amount is None
        # Không dấu bẩn: không có giá thì không có gì để tính lại.
        assert [
            mark for mark in marks_of_branch(session, context.branch_id) if mark.item_id == item_id
        ] == []

    run(work)


def test_custodial_stock_is_reported_apart_from_owned_stock(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-split")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=APR_5,
            quantity=Decimal(100),
            unit_cost=Decimal(20_000),
        )
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(60)
        )

        rows = [
            row
            for row in stock_rows(session, as_of=APR_15, branch_id=context.branch_id)
            if row.item_id == item_id
        ]
        assert len(rows) == 1, "một khóa có cả hai loại phải ra MỘT dòng"
        assert rows[0].on_hand == Decimal(100)
        assert rows[0].custodial_qty == Decimal(60)
        # Giá trị chỉ tính hàng của mình — 100 × 20.000.
        assert rows[0].value == Decimal("2000000.00")

    run(work)


@pytest.mark.parametrize("method", ["wavg_moving", "fifo", "wavg_period", "specific"])
def test_every_valuation_method_ignores_custodial_quantity(
    run: Runner, context: PostingContext, accounts: dict[str, int], method: str
) -> None:
    """Khóa trộn hai loại: giá xuất phải là giá của 100 cái hàng của mình, không
    phải giá bình quân pha loãng bởi 60 cái giữ hộ (giữ hộ không có giá trị)."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, f"custodial-{method}")
        set_valuation_method(session, context, method)
        try:
            receipt_id = post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=APR_5,
                quantity=Decimal(100),
                unit_cost=Decimal(20_000),
            )
            post_custodial_receipt(
                session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(60)
            )
            source_id = None
            if method == "specific":
                source_id = next(
                    movement.id
                    for movement in movements_of(session, receipt_id)
                    if movement.direction == MovementDirection.IN
                )
            issue_id = post_issue(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=APR_10,
                quantity=Decimal(30),
                source_movement_id=source_id,
            )
            run_engine(session, context)
            session.expire_all()
            issued = [
                movement
                for movement in movements_of(session, issue_id)
                if movement.direction == MovementDirection.OUT
            ]
            assert len(issued) == 1
            assert issued[0].unit_cost == Decimal("20000.000000")
            assert issued[0].amount == Decimal("600000.00")
        finally:
            set_valuation_method(session, context, "wavg_moving")

    run(work)


def test_period_lock_allows_a_period_that_only_has_custodial_movements(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-lock")
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(15)
        )
        run_engine(session, context)
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        period = session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.fiscal_year_id == year.id,
                AccountingPeriod.start_date <= APR_5,
                AccountingPeriod.end_date >= APR_5,
            )
            .limit(1)
        )
        assert period is not None
        # Không ném: dòng giữ hộ là "đã chốt", không phải "chưa tính giá".
        ensure_inventory_costed(session, period, year)

    run(work)


def test_period_lock_still_blocks_when_an_owned_line_is_uncosted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Chiều ngược của bài trên: `NOT_APPLICABLE` không được nới lỏng mục kiểm
    cho dòng thật sự chưa tính giá."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-lock-negative")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=APR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(5_000),
        )
        post_issue(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=APR_10,
            quantity=Decimal(4),
        )
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        period = session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.fiscal_year_id == year.id,
                AccountingPeriod.start_date <= APR_10,
                AccountingPeriod.end_date >= APR_10,
            )
            .limit(1)
        )
        assert period is not None
        with pytest.raises(PeriodLockChecklistError):
            ensure_inventory_costed(session, period, year)

    run(work)


def test_custodial_shortage_is_not_covered_by_owned_stock(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "block")
        item_id = fresh_item(session, "custodial-guard")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=APR_5,
            quantity=Decimal(500),
            unit_cost=Decimal(1_000),
        )
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(10)
        )
        service = InventoryVoucherService(session)
        voucher = service.create(
            custodial_issue_payload(
                context, posting_date=APR_10, quantity=Decimal(25), item_id=item_id
            ),
            user_id=ACTOR_ID,
        )
        try:
            with pytest.raises(PostingValidationError) as excinfo:
                service.post(voucher.id, user_id=ACTOR_ID, acknowledged_warnings=True)
            (violation,) = excinfo.value.violations
            assert violation.code == STOCK_NEGATIVE_CODE
            # Thiếu 15 của phần giữ hộ, KHÔNG được bù bằng 500 hàng của mình.
            assert Decimal(str(violation.details["projected"])) == Decimal(-15)
            assert violation.details["is_custodial"] is True
        finally:
            set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "warn")

    run(work)


def test_custodial_line_refuses_accounts(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-accounts")
        payload = InventoryVoucherIn(
            kind=InventoryVoucherKind.RECEIPT,
            operation_code="nhap-khac",
            warehouse_id=MAIN_WAREHOUSE_ID,
            branch_id=context.branch_id,
            document_date=APR_5,
            posting_date=APR_5,
            currency_code="VND",
            exchange_rate=Decimal(1),
            lines=(
                InventoryVoucherLineIn(
                    item_id=item_id,
                    unit_id=UNIT_PIECE_ID,
                    quantity=Decimal(5),
                    is_custodial=True,
                ),
            ),
        )
        # Lách lớp Pydantic: phiếu sinh tự động không đi qua nó, nên service
        # phải canh lần hai.
        line = payload.lines[0].model_construct(
            **{
                **payload.lines[0].model_dump(),
                "debit_account_id": accounts["156"],
                "credit_account_id": accounts["154"],
            }
        )
        with pytest.raises(PostingValidationError) as excinfo:
            InventoryVoucherService(session).create(
                payload.model_copy(update={"lines": (line,)}), user_id=ACTOR_ID
            )
        assert any(
            violation.code == CUSTODIAL_LINE_HAS_ACCOUNTS_CODE
            for violation in excinfo.value.violations
        )

    run(work)


def test_assembly_refuses_a_custodial_line(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        component = fresh_item(session, "custodial-assembly-part")
        product = fresh_item(session, "custodial-assembly-product")
        payload = InventoryVoucherIn(
            kind=InventoryVoucherKind.ASSEMBLY,
            operation_code="lap-rap",
            warehouse_id=MAIN_WAREHOUSE_ID,
            branch_id=context.branch_id,
            document_date=APR_5,
            posting_date=APR_5,
            currency_code="VND",
            exchange_rate=Decimal(1),
            lines=(
                InventoryVoucherLineIn(
                    item_id=component,
                    unit_id=UNIT_PIECE_ID,
                    quantity=Decimal(2),
                    is_custodial=True,
                ),
                InventoryVoucherLineIn(
                    item_id=product,
                    unit_id=UNIT_PIECE_ID,
                    quantity=Decimal(1),
                    is_product=True,
                ),
            ),
        )
        with pytest.raises(PostingValidationError) as excinfo:
            InventoryVoucherService(session).create(payload, user_id=ACTOR_ID)
        assert any(
            violation.code == CUSTODIAL_KIND_NOT_ALLOWED_CODE
            for violation in excinfo.value.violations
        )

    run(work)


def test_unposting_a_custodial_voucher_leaves_no_recalc_mark(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-unpost")
        voucher_id = post_custodial_receipt(
            session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(8)
        )
        InventoryVoucherService(session).unpost(voucher_id, user_id=ACTOR_ID)
        assert movements_of(session, voucher_id) == []
        assert [
            mark for mark in marks_of_branch(session, context.branch_id) if mark.item_id == item_id
        ] == []

    run(work)


def test_stock_rows_reports_a_custodial_only_key_with_zero_value(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Khóa CHỈ có hàng giữ hộ: giá trị là 0 đã biết, không phải `None` "chưa
    tính" — `bool_and(...) FILTER` trả NULL ở đây và phải được đọc đúng."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "custodial-only")
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=APR_5, quantity=Decimal(12)
        )
        rows = [
            row
            for row in stock_rows(session, as_of=APR_15, branch_id=context.branch_id)
            if row.item_id == item_id
        ]
        assert len(rows) == 1
        assert rows[0].on_hand == Decimal(0)
        assert rows[0].custodial_qty == Decimal(12)
        assert rows[0].value == Decimal(0)

    run(work)
