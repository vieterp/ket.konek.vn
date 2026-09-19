"""Spike 8B (phase-08 bước 9, ADR-014): tính giá xuất kho trên 100.000 dòng
sổ kho / 5.000 mã hàng / 12 kỳ — bình quân tức thời rồi FIFO — dưới **5 phút**
mỗi phương pháp, gồm cả ghi lại giá vốn (`PostingService.repost`) và dựng
snapshot. Cổng dừng-cả-dây của phase 8: trượt thì tối ưu trước khi làm 8C.

Cùng quy ước spike S5 (`test_recalc_performance`): dữ liệu sinh set-based bằng
`generate_series` — phiếu THẬT trong `vouchers`/`inventory_vouchers`/
`inventory_voucher_lines`/`inventory_movements` để repost đo được — mốc là hằng
số module cố ý cao hơn số đo thật, số đo in ra stdout (`pytest -s`) để chép vào
phase file. Khẳng định việc ĐÃ xảy ra: 0 dòng chờ giá, Σ giá vốn 632/621 trên
sổ cái = Σ `amount` xuất — một vòng lặp rỗng không qua được đây.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    run_engine,
    seed_inventory_package_data,
    set_valuation_method,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import FiscalYear, InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.models import CostState, InventoryMovement, MovementDirection
from ket.posting.contracts import Voucher
from ket.posting.engine.models import GlPosting, Ledger
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
ITEM_COUNT: Final[int] = 5_000
ROUNDS: Final[int] = 10  # mỗi mã hàng: 10 lần nhập + 10 lần xuất → 100.000 movement
# Nhập 20 @ (100 + n), xuất 10: tồn tích lũy nên FIFO (ăn lớp cũ) ≠ bình quân từ
# vòng 2 — lượt FIFO phải ghi lại ≥ 45.000 dòng và repost, không phải "đổi 0".
LINES_PER_VOUCHER: Final[int] = 5
BUDGET_SECONDS: Final[float] = 300.0
TAG: Final[str] = "S8B"

SEED_ITEMS_SQL = """
INSERT INTO items (uid, code, name, path, nature, base_unit_id)
SELECT gen_random_uuid(), :tag || '-' || n, 'Spike 8B ' || n, '0.', 'goods', :unit_id
FROM generate_series(1, :item_count) AS n
"""
FIX_ITEM_PATHS_SQL = (
    "UPDATE items SET path = id::text || '.' WHERE code LIKE :pattern AND path = '0.'"
)

SEED_VOUCHERS_SQL = """
INSERT INTO vouchers (
    id, document_type, voucher_no, branch_id, document_date, posting_date, period_id,
    currency_code, exchange_rate, status, created_at, created_by, posted_at, posted_by,
    row_version
)
SELECT gen_random_uuid(), t.doc, t.doc || '-' || :tag || '-' || g || '-' || n, :branch_id,
       d.day, d.day, p.id, 'VND', 1, 2, now(), :actor_id, now(), :actor_id, 0
FROM generate_series(1, :groups) AS g
CROSS JOIN generate_series(1, :rounds) AS n
CROSS JOIN (VALUES ('NK', 0), ('XK', 18)) AS t(doc, day_offset)
CROSS JOIN LATERAL (
    SELECT CAST(:year_start AS DATE) + (n - 1) * 36 + t.day_offset AS day
) AS d
JOIN accounting_periods p
  ON p.fiscal_year_id = :fiscal_year_id AND d.day BETWEEN p.start_date AND p.end_date
"""

SEED_BODIES_SQL = """
INSERT INTO inventory_vouchers (id, kind, operation_code, warehouse_id)
SELECT v.id,
       CASE v.document_type WHEN 'NK' THEN 0 ELSE 1 END,
       CASE v.document_type WHEN 'NK' THEN 'nhap-thanh-pham' ELSE 'xuat-nvl-san-xuat' END,
       :warehouse_id
FROM vouchers v
WHERE v.voucher_no LIKE :pattern
"""

SEED_LINES_SQL = """
INSERT INTO inventory_voucher_lines (
    id, voucher_id, line_no, item_id, warehouse_id, unit_id, quantity, base_quantity,
    unit_cost_fc, amount_fc, debit_account_id, credit_account_id
)
SELECT gen_random_uuid(), v.id, k, i.id, :warehouse_id, :unit_id,
       CASE WHEN v.document_type = 'NK' THEN 20 ELSE 10 END,
       CASE WHEN v.document_type = 'NK' THEN 20 ELSE 10 END,
       CASE WHEN v.document_type = 'NK' THEN 100 + v.n END,
       CASE WHEN v.document_type = 'NK' THEN 20 * (100 + v.n) END,
       CASE WHEN v.document_type = 'NK' THEN CAST(:receipt_debit AS INTEGER)
            ELSE CAST(:issue_debit AS INTEGER) END,
       CASE WHEN v.document_type = 'NK' THEN CAST(:receipt_credit AS INTEGER)
            ELSE CAST(:issue_credit AS INTEGER) END
FROM (
    SELECT id, document_type,
           CAST(split_part(voucher_no, '-', 3) AS INTEGER) AS g,
           CAST(split_part(voucher_no, '-', 4) AS INTEGER) AS n
    FROM vouchers
    WHERE voucher_no LIKE :pattern
) AS v
CROSS JOIN generate_series(1, :lines_per_voucher) AS k
JOIN items i ON i.code = :tag || '-' || ((v.g - 1) * :lines_per_voucher + k)
"""

SEED_MOVEMENTS_SQL = """
INSERT INTO inventory_movements (
    voucher_id, line_id, branch_id, warehouse_id, item_id, lot_key, posting_date, period_id,
    sequence_in_day, direction, quantity, unit_cost, amount, cost_state
)
SELECT v.id, l.id, v.branch_id, l.warehouse_id, l.item_id, 0, v.posting_date, v.period_id, 1,
       CASE v.document_type WHEN 'NK' THEN 1 ELSE -1 END,
       l.base_quantity, l.unit_cost_fc, l.amount_fc,
       CASE v.document_type WHEN 'NK' THEN 1 ELSE 0 END
FROM vouchers v
JOIN inventory_voucher_lines l ON l.voucher_id = v.id
WHERE v.voucher_no LIKE :pattern
"""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    return seed_inventory_package_data(session_factory, dataset_alpha, context)


@pytest.fixture(scope="module")
def seeded(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> int:
    """Gieo một lần cho cả module; trả số movement đã gieo."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    pattern = f"%-{TAG}-%"
    with unit_of_work(session_factory, scope) as session:
        already = session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .join(Voucher, Voucher.id == InventoryMovement.voucher_id)
            .where(Voucher.voucher_no.like(pattern))
        )
        if already:
            return int(already)
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        session.execute(
            text(SEED_ITEMS_SQL), {"tag": TAG, "unit_id": UNIT_PIECE_ID, "item_count": ITEM_COUNT}
        )
        session.execute(text(FIX_ITEM_PATHS_SQL), {"pattern": f"{TAG}-%"})
        session.execute(
            text(SEED_VOUCHERS_SQL),
            {
                "tag": TAG,
                "branch_id": context.branch_id,
                "actor_id": ACTOR_ID,
                "groups": ITEM_COUNT // LINES_PER_VOUCHER,
                "rounds": ROUNDS,
                "year_start": year.start_date,
                "fiscal_year_id": year.id,
            },
        )
        session.execute(
            text(SEED_BODIES_SQL), {"pattern": pattern, "warehouse_id": MAIN_WAREHOUSE_ID}
        )
        session.execute(
            text(SEED_LINES_SQL),
            {
                "pattern": pattern,
                "tag": TAG,
                "warehouse_id": MAIN_WAREHOUSE_ID,
                "unit_id": UNIT_PIECE_ID,
                "lines_per_voucher": LINES_PER_VOUCHER,
                "receipt_debit": accounts["155"],
                "receipt_credit": accounts["154"],
                "issue_debit": accounts["621"],
                "issue_credit": accounts["152"],
            },
        )
        session.execute(text(SEED_MOVEMENTS_SQL), {"pattern": pattern})
        count = session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .join(Voucher, Voucher.id == InventoryMovement.voucher_id)
            .where(Voucher.voucher_no.like(pattern))
        )
        return int(count or 0)


def _spike_issue_totals(session: Session, cost_state: int | None = None) -> tuple[int, Decimal]:
    query = (
        select(func.count(), func.coalesce(func.sum(InventoryMovement.amount), 0))
        .select_from(InventoryMovement)
        .join(Voucher, Voucher.id == InventoryMovement.voucher_id)
        .where(Voucher.voucher_no.like(f"%-{TAG}-%"))
        .where(InventoryMovement.direction == MovementDirection.OUT)
    )
    if cost_state is not None:
        query = query.where(InventoryMovement.cost_state == cost_state)
    count, total = session.execute(query).one()
    return int(count), Decimal(total)


def _cogs_on_ledger(session: Session, account_id: int) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(GlPosting.debit), 0))
        .select_from(GlPosting)
        .join(Voucher, Voucher.id == GlPosting.voucher_id)
        .where(Voucher.voucher_no.like(f"%-{TAG}-%"))
        .where(GlPosting.ledger == Ledger.FINANCIAL, GlPosting.account_id == account_id)
    )
    return Decimal(total or 0)


@pytest.mark.parametrize(
    "method",
    [InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING, InventoryValuationMethod.FIFO],
)
def test_spike_costing_one_hundred_thousand_movements_under_budget(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    seeded: int,
    method: InventoryValuationMethod,
) -> None:
    assert seeded == ITEM_COUNT * ROUNDS * 2
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    year_start = date(2026, 1, 1)
    with unit_of_work(session_factory, scope) as session:
        set_valuation_method(session, context, method.value)
    try:
        started = time.monotonic()
        with unit_of_work(session_factory, scope) as session:
            # Lượt đầu (wavg): tính mọi dòng chờ giá. Lượt sau (fifo): ép tính lại
            # cả năm — dòng nào đổi giá thì ghi lại giá vốn.
            result = run_engine(session, context, force_from=year_start)
        elapsed = time.monotonic() - started
        print(  # noqa: T201 — số đo cho báo cáo phase
            f"\nspike 8B [{method.value}]: {seeded} movement / {ITEM_COUNT} mã hàng = "
            f"{elapsed:.1f}s (vòng {result.passes}, đổi {result.movements_updated}, "
            f"repost {result.vouchers_reposted} chứng từ; mốc < {BUDGET_SECONDS:.0f}s)"
        )
        assert elapsed < BUDGET_SECONDS
        assert result.movements_updated >= ITEM_COUNT * (ROUNDS - 1)
        assert result.vouchers_reposted >= ITEM_COUNT // LINES_PER_VOUCHER * (ROUNDS - 1)
        with unit_of_work(session_factory, scope) as session:
            pending, _ = _spike_issue_totals(session, CostState.PENDING)
            costed, issued_value = _spike_issue_totals(session, CostState.COSTED)
            assert pending == 0
            assert costed == ITEM_COUNT * ROUNDS
            assert _cogs_on_ledger(session, accounts["621"]) == issued_value
            assert issued_value > 0
    finally:
        with unit_of_work(session_factory, scope) as session:
            set_valuation_method(
                session, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value
            )
