"""Spike S5 (RT-22, ADR-014): recalc + đọc theo chiều trên 100.000 dòng thật.

Cổng chặn của phase 4 trước khi viết phân hệ kho: nếu các con số dưới đây
trượt, phải tối ưu index/partition (hoặc thêm rollup có mục tiêu) TRƯỚC khi
nhân bản kiến trúc ra 13 module — không phải sau.

* Tính lại 12 kỳ trên 100.000 dòng `gl_postings`: **< 60s** (mốc phase-04
  bước 11; FR-NFR-042 gián tiếp).
* Đọc theo chiều thẳng từ `gl_postings` cho một kỳ: **< 10s** (FR-NFR-041) —
  dữ liệu sinh DÀY chiều (200 đối tác × 50 đối tượng THCP × 30 công trình ×
  40 khoản mục) đúng yêu cầu RT-22, vì đường theo-chiều không có snapshot đỡ.
* Bảng cân đối TK đường tính-thẳng (kỳ bẩn): **< 10s** (FR-NFR-041).

Toàn bộ dữ liệu sinh bằng set-based SQL (`generate_series`) — vừa nhanh vừa là
chính phép chứng minh LD-14: không vòng lặp Python nào chạm dòng dữ liệu, kể
cả trong test. Ngưỡng cố ý CAO hơn nhiều số đo thật (máy CI yếu hơn máy dev);
số đo thật in ra stdout để báo cáo phase chép lại (`pytest -s`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.balances import recalc
from ket.posting.balances.query_service import trial_balance
from ket.posting.engine.models import Ledger
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VOUCHER_COUNT = 10_000
LINES_PER_VOUCHER = 10  # 5 cặp Nợ/Có cân nhau → 100.000 dòng, mỗi chứng từ cân

RECALC_BUDGET_SECONDS = 60.0
QUERY_BUDGET_SECONDS = 10.0

SEED_VOUCHERS_SQL = """
INSERT INTO vouchers (
    id, document_type, voucher_no, branch_id, document_date, posting_date,
    period_id, currency_code, exchange_rate, status, created_at, created_by,
    posted_at, posted_by, row_version
)
SELECT gen_random_uuid(), 'GLE', 'S5-' || n, :branch_id,
       p.start_date, p.start_date, p.id, 'VND', 1, 2, now(), :actor_id,
       now(), :actor_id, 0
FROM generate_series(1, :voucher_count) AS n
JOIN accounting_periods p
  ON p.fiscal_year_id = :fiscal_year_id
 AND p.period_no = ((n - 1) % 12) + 1
"""

SEED_POSTINGS_SQL = """
INSERT INTO gl_postings (
    voucher_id, line_no, ledger, branch_id, posting_date, period_id,
    account_id, currency_code, exchange_rate, debit_fc, credit_fc, debit, credit,
    partner_id, partner_kind, cost_object_id, project_id, expense_item_id,
    description
)
SELECT v.id, line_no, :ledger, v.branch_id, v.posting_date, v.period_id,
       CASE (v.seq + line_no) % 4
           WHEN 0 THEN CAST(:acc_0 AS INTEGER)
           WHEN 1 THEN CAST(:acc_1 AS INTEGER)
           WHEN 2 THEN CAST(:acc_2 AS INTEGER)
           ELSE CAST(:acc_3 AS INTEGER)
       END,
       'VND', 1,
       CASE WHEN line_no % 2 = 1 THEN 100000 ELSE 0 END,
       CASE WHEN line_no % 2 = 0 THEN 100000 ELSE 0 END,
       CASE WHEN line_no % 2 = 1 THEN 100000 ELSE 0 END,
       CASE WHEN line_no % 2 = 0 THEN 100000 ELSE 0 END,
       (v.seq % 200) + 1, 0,
       ((v.seq + line_no) % 50) + 1,
       ((v.seq * 3 + line_no) % 30) + 1,
       ((v.seq * 7 + line_no) % 40) + 1,
       'spike S5'
FROM (
    SELECT id, branch_id, posting_date, period_id,
           row_number() OVER (ORDER BY voucher_no) AS seq
    FROM vouchers
    WHERE document_type = 'GLE' AND voucher_no LIKE 'S5-%' AND branch_id = :branch_id
) AS v
CROSS JOIN generate_series(1, :lines_per_voucher) AS line_no
"""

DIMENSION_READ_SQL = """
SELECT cost_object_id, expense_item_id,
       SUM(debit) AS total_debit, SUM(credit) AS total_credit
FROM gl_postings
WHERE ledger = :ledger
  AND branch_id = :branch_id
  AND period_id = :period_id
  AND project_id = :project_id
GROUP BY cost_object_id, expense_item_id
"""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture(scope="module")
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


@pytest.fixture(scope="module")
def seeded(run: Runner, context: PostingContext) -> int:
    """Sinh 100.000 dòng phát sinh dày chiều; trả về số dòng đã sinh."""

    def work(session: Session) -> object:
        session.execute(
            text(SEED_VOUCHERS_SQL),
            {
                "branch_id": context.branch_id,
                "actor_id": ACTOR_ID,
                "voucher_count": VOUCHER_COUNT,
                "fiscal_year_id": context.fiscal_year_id,
            },
        )
        session.execute(
            text(SEED_POSTINGS_SQL),
            {
                "ledger": Ledger.FINANCIAL.value,
                "branch_id": context.branch_id,
                "lines_per_voucher": LINES_PER_VOUCHER,
                "acc_0": context.accounts["642"],
                "acc_1": context.accounts["111"],
                "acc_2": context.accounts["112"],
                "acc_3": context.accounts["511"],
            },
        )
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM gl_postings WHERE branch_id = :branch_id"
                " AND description = 'spike S5'"
            ),
            {"branch_id": context.branch_id},
        ).scalar_one()
        return count

    count = run(work)
    assert isinstance(count, int)
    return count


def test_spike_s5_recalc_twelve_periods_under_budget(
    run: Runner, context: PostingContext, seeded: int
) -> None:
    assert seeded == VOUCHER_COUNT * LINES_PER_VOUCHER

    def work(session: Session) -> object:
        first_period = session.execute(
            text(
                "SELECT id FROM accounting_periods"
                " WHERE fiscal_year_id = :year ORDER BY period_no LIMIT 1"
            ),
            {"year": context.fiscal_year_id},
        ).scalar_one()
        runs = recalc.build_runs(session, (), forced=((Ledger.FINANCIAL.value, first_period),))
        started = time.monotonic()
        done = 0
        for one_run in runs:
            for period in one_run.periods:
                recalc.recalc_period(
                    session, ledger=one_run.ledger, branch_id=context.branch_id, period=period
                )
                done += 1
        elapsed = time.monotonic() - started
        print(f"\nspike S5: recalc {done} kỳ / {seeded} dòng = {elapsed:.2f}s")  # noqa: T201 — số đo cho báo cáo phase
        assert done == 12
        assert elapsed < RECALC_BUDGET_SECONDS, f"recalc 12 kỳ mất {elapsed:.2f}s (mốc < 60s)"
        # Recalc phải THẬT SỰ ghi snapshot — đo một vòng lặp không làm gì
        # cũng "đạt mốc". 4 TK × 1 tiền tệ × 12 kỳ của chi nhánh spike.
        snapshot_rows = session.execute(
            text(
                "SELECT COUNT(*) FROM account_balances"
                " WHERE branch_id = :branch_id AND ledger = :ledger"
            ),
            {"branch_id": context.branch_id, "ledger": Ledger.FINANCIAL.value},
        ).scalar_one()
        assert snapshot_rows == 48
        return None

    run(work)


def test_spike_s5_dimension_read_under_budget(
    run: Runner, context: PostingContext, seeded: int
) -> None:
    """Đường đọc theo chiều KHÔNG có snapshot đỡ — phải sống bằng index."""

    def work(session: Session) -> object:
        period_id = session.execute(
            text(
                "SELECT id FROM accounting_periods WHERE fiscal_year_id = :year AND period_no = 6"
            ),
            {"year": context.fiscal_year_id},
        ).scalar_one()
        started = time.monotonic()
        rows = session.execute(
            text(DIMENSION_READ_SQL),
            {
                "ledger": Ledger.FINANCIAL.value,
                "branch_id": context.branch_id,
                "period_id": period_id,
                "project_id": 7,
            },
        ).all()
        elapsed = time.monotonic() - started
        print(f"\nspike S5: đọc theo chiều 1 kỳ = {elapsed:.3f}s ({len(rows)} nhóm)")  # noqa: T201 — số đo cho báo cáo phase
        assert rows, "dữ liệu dày chiều phải cho ra ít nhất một nhóm"
        assert elapsed < QUERY_BUDGET_SECONDS
        return None

    run(work)


def test_spike_s5_trial_balance_direct_path_under_budget(
    run: Runner, context: PostingContext, seeded: int
) -> None:
    """Bảng cân đối TK đường tính-thẳng (kỳ bẩn) trên 100k dòng < 10s."""

    def work(session: Session) -> object:
        period = session.execute(
            text(
                "SELECT id FROM accounting_periods WHERE fiscal_year_id = :year AND period_no = 12"
            ),
            {"year": context.fiscal_year_id},
        ).scalar_one()
        # Ép đường tính-thẳng bằng dấu bẩn thật — đúng nhánh mà kỳ bẩn sẽ đi.
        from ket.posting.balances.recalc_queue import mark_dirty

        first_period = session.execute(
            text(
                "SELECT id FROM accounting_periods WHERE fiscal_year_id = :year AND period_no = 1"
            ),
            {"year": context.fiscal_year_id},
        ).scalar_one()
        mark_dirty(
            session,
            ledger=Ledger.FINANCIAL.value,
            branch_id=context.branch_id,
            from_period_id=first_period,
            reason="spike S5",
        )
        started = time.monotonic()
        result = trial_balance(
            session, ledger=Ledger.FINANCIAL, period_id=period, branch_id=context.branch_id
        )
        elapsed = time.monotonic() - started
        print(f"\nspike S5: bảng cân đối tính-thẳng kỳ 12 = {elapsed:.3f}s")  # noqa: T201 — số đo cho báo cáo phase
        assert result.stale is True
        assert result.rows
        total_debit = sum((row.period_debit for row in result.rows), Decimal(0))
        total_credit = sum((row.period_credit for row in result.rows), Decimal(0))
        assert total_debit == total_credit
        assert elapsed < QUERY_BUDGET_SECONDS
        # Dọn dấu để không làm bẩn test khác cùng chi nhánh (module-scope).
        marks = recalc.pending_marks(session, branch_id=context.branch_id)
        recalc.clear_marks(session, branch_id=context.branch_id, marks=marks)
        return None

    run(work)
