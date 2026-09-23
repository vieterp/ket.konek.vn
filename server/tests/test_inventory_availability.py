"""Cột **"Có thể bán"** (U7, lát 8D) trên PostgreSQL thật.

`CommitmentProvider` do `sales` cài: đã hứa giao = hóa đơn bán **chưa rời kho**
— đã cất chưa ghi sổ, hoặc đã ghi sổ mà chưa có phiếu kho nào sinh ra từ nó
(hệ thống không có chứng từ đơn đặt hàng, xem `sales/commitment.py`).

* Hóa đơn nháp làm `available_to_promise` tụt; ghi sổ (sinh phiếu XK) làm tồn
  tụt và cam kết **về 0** — không trừ hai lần.
* Hóa đơn không khai kho không được gán cho kho nào.
* Cam kết của kho B không bị kho A gánh (hỏi cổng một lượt mỗi kho).
* Cam kết cộng dồn đúng khi một mã hàng có nhiều dòng tồn (nhiều lô).
* Không phân hệ nào cài cổng → `committed = 0` và `has_commitment_source` false.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    fresh_item,
    post_custodial_receipt,
    post_receipt,
    seed_inventory_package_data,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import PROVIDERS
from ket.modules.inventory.availability import availability
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
CUSTOMER_ID = 908_301
SALESPERSON_ID = 908_302
JUL_1 = date(2026, 7, 1)
JUL_10 = date(2026, 7, 10)


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-8D-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-8D-01")
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


def _sale(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    quantity: Decimal,
    posting_date: date = JUL_10,
    is_stock_issue: bool = True,
    warehouse_id: int | None = MAIN_WAREHOUSE_ID,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=0,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        invoice_no="0008801",
        description="bán hàng cho bài Có thể bán",
        is_stock_issue=is_stock_issue,
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                item_id=item_id,
                unit_id=UNIT_PIECE_ID,
                warehouse_id=warehouse_id,
                quantity=quantity,
                unit_price_fc=Decimal(150_000),
                amount_fc=Decimal(150_000) * quantity,
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(15_000) * quantity,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                cogs_account_id=accounts["632"],
                inventory_account_id=accounts["156"],
            ),
        ),
    )


def _row_of(session: Session, context: PostingContext, item_id: int) -> object:
    response = availability(session, as_of=JUL_10, branch_id=context.branch_id, item_ids=(item_id,))
    assert response.has_commitment_source is True
    assert len(response.items) == 1
    return response.items[0]


def test_a_saved_invoice_commits_stock_and_posting_it_releases_the_commitment(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-basic")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUL_1,
            quantity=Decimal(100),
            unit_cost=Decimal(50_000),
        )
        before = _row_of(session, context, item_id)
        assert before.on_hand == Decimal(100)  # type: ignore[attr-defined]
        assert before.committed == Decimal(0)  # type: ignore[attr-defined]
        assert before.available_to_promise == Decimal(100)  # type: ignore[attr-defined]

        sales = SalesInvoiceService(session)
        invoice = sales.create(
            _sale(context, accounts, item_id=item_id, quantity=Decimal(30)), user_id=ACTOR_ID
        )
        saved = _row_of(session, context, item_id)
        assert saved.on_hand == Decimal(100), "chưa ghi sổ thì tồn chưa đổi"  # type: ignore[attr-defined]
        assert saved.committed == Decimal(30)  # type: ignore[attr-defined]
        assert saved.available_to_promise == Decimal(70)  # type: ignore[attr-defined]

        sales.post(invoice.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        posted = _row_of(session, context, item_id)
        # Hàng đã rời kho: tồn tụt, cam kết về 0 — KHÔNG trừ hai lần.
        assert posted.on_hand == Decimal(70)  # type: ignore[attr-defined]
        assert posted.committed == Decimal(0)  # type: ignore[attr-defined]
        assert posted.available_to_promise == Decimal(70)  # type: ignore[attr-defined]

    run(work)


def test_an_invoice_without_a_warehouse_commits_nothing_to_any_warehouse(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn không khai kho là một lời hứa THẬT, nhưng nó không thuộc kho nào
    — gán nó cho một kho bất kỳ là bịa. Nó vẫn hiện ở nhóm "chưa xuất kho" của
    tab việc còn thiếu, nơi nó có nghĩa."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-nostock")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUL_1,
            quantity=Decimal(40),
            unit_cost=Decimal(10_000),
        )
        sales = SalesInvoiceService(session)
        invoice = sales.create(
            _sale(
                context,
                accounts,
                item_id=item_id,
                quantity=Decimal(9),
                is_stock_issue=False,
                warehouse_id=None,
            ),
            user_id=ACTOR_ID,
        )
        assert invoice is not None
        row = _row_of(session, context, item_id)
        assert row.committed == Decimal(0)  # type: ignore[attr-defined]
        assert row.available_to_promise == Decimal(40)  # type: ignore[attr-defined]

    run(work)


def test_commitment_is_attributed_to_the_warehouse_that_owes_it(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cam kết của kho B không được kho A gánh. Trước khi hỏi cổng theo từng
    kho, lượt chia theo thứ tự dòng làm kho sắp trước nuốt lời hứa của kho sau
    — lưới hiện đúng ngược (review 8D H-3)."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-warehouses")
        for warehouse_id in (MAIN_WAREHOUSE_ID, SECOND_WAREHOUSE_ID):
            post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=JUL_1,
                quantity=Decimal(10),
                unit_cost=Decimal(1_000),
                warehouse_id=warehouse_id,
            )
        SalesInvoiceService(session).create(
            _sale(
                context,
                accounts,
                item_id=item_id,
                quantity=Decimal(10),
                warehouse_id=SECOND_WAREHOUSE_ID,
            ),
            user_id=ACTOR_ID,
        )
        response = availability(
            session, as_of=JUL_10, branch_id=context.branch_id, item_ids=(item_id,)
        )
        by_warehouse = {row.warehouse_id: row for row in response.items}
        assert by_warehouse[MAIN_WAREHOUSE_ID].committed == Decimal(0)
        assert by_warehouse[MAIN_WAREHOUSE_ID].available_to_promise == Decimal(10)
        assert by_warehouse[SECOND_WAREHOUSE_ID].committed == Decimal(10)
        assert by_warehouse[SECOND_WAREHOUSE_ID].available_to_promise == Decimal(0)

    run(work)


def test_commitment_spreads_across_the_lots_of_one_item(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cam kết là số theo MÃ HÀNG; lưới tồn chia nó cho các lô và tổng phải
    bằng đúng số đã hứa — không nhân lên theo số dòng."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-lots")
        for lot_no, quantity in (("L1", Decimal(10)), ("L2", Decimal(10))):
            post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=JUL_1,
                quantity=quantity,
                unit_cost=Decimal(1_000),
                lot_no=lot_no,
            )
        SalesInvoiceService(session).create(
            _sale(context, accounts, item_id=item_id, quantity=Decimal(15)), user_id=ACTOR_ID
        )
        response = availability(
            session, as_of=JUL_10, branch_id=context.branch_id, item_ids=(item_id,)
        )
        assert len(response.items) == 2
        assert sum(row.committed for row in response.items) == Decimal(15)
        assert sum(row.on_hand for row in response.items) == Decimal(20)
        assert sum(row.available_to_promise for row in response.items) == Decimal(5)

    run(work)


def test_custodial_quantity_is_reported_but_never_promised(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-custodial")
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=JUL_1, quantity=Decimal(80)
        )
        row = _row_of(session, context, item_id)
        assert row.custodial_qty == Decimal(80)  # type: ignore[attr-defined]
        assert row.on_hand == Decimal(0)  # type: ignore[attr-defined]
        assert row.available_to_promise == Decimal(0)  # type: ignore[attr-defined]

    run(work)


def test_without_a_registered_provider_commitment_is_zero(
    run: Runner, context: PostingContext, accounts: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "atp-noprovider")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUL_1,
            quantity=Decimal(12),
            unit_cost=Decimal(1_000),
        )
        SalesInvoiceService(session).create(
            _sale(context, accounts, item_id=item_id, quantity=Decimal(5)), user_id=ACTOR_ID
        )
        monkeypatch.setattr(PROVIDERS, "commitment_providers", tuple)
        response = availability(
            session, as_of=JUL_10, branch_id=context.branch_id, item_ids=(item_id,)
        )
        assert response.has_commitment_source is False
        assert response.items[0].committed == Decimal(0)
        assert response.items[0].available_to_promise == Decimal(12)

    run(work)
