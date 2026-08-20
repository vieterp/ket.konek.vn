"""FR-NFR-041 (bước 19 phase-05): báo cáo 1 kỳ trên năm 100.000 chứng từ < 10s.

Cùng quy ước với spike S5 (`test_recalc_performance`): dữ liệu sinh set-based
bằng `generate_series` (LD-14 — không vòng lặp Python nào chạm dòng), mốc là
hằng số module CAO hơn số đo thật để CI yếu vẫn qua, số đo thật in ra stdout
cho báo cáo phase (`pytest -s`).

Ba phép đo, ba đường mà người dùng thật chờ:

* **XLSX một kỳ** trên `S03b-DN` (Sổ Cái — dataset nặng nhất: mỗi dòng phát
  sinh một dòng sổ) — đường "lấy đủ dữ liệu" của phase-05.
* **Preview lưới** cùng kỳ — đường nhìn trên màn hình (trần 2.000 dòng trình
  bày nhưng vẫn phải quét + nhóm trọn kỳ).
* **COUNT ước lượng** — lượt đếm mới chạy TRƯỚC MỌI lượt render (ngưỡng
  chuyển-job, lát 5E) nên bản thân nó không được là gánh nặng.

PDF một kỳ dày cố ý KHÔNG đo ở đây: vượt ngưỡng `report.pdf_job_threshold_rows`
nó đi job nền theo thiết kế (ADR-009 rebaseline sổ dài ≤ 4 phút job nền);
mốc 10s của FR-NFR-041 áp cho các đường đồng bộ còn lại.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, date, datetime
from typing import Final

import openpyxl
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.engine.models import Ledger
from ket.reporting.engine.engine import estimate_report_rows, preview_report, render_report
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VOUCHER_COUNT: Final[int] = 100_000
LINES_PER_VOUCHER: Final[int] = 2  # một cặp Nợ/Có — 200.000 dòng phát sinh cả năm
RENDER_BUDGET_SECONDS: Final[float] = 10.0
COUNT_BUDGET_SECONDS: Final[float] = 1.0

SEED_VOUCHERS_SQL = """
INSERT INTO vouchers (
    id, document_type, voucher_no, branch_id, document_date, posting_date,
    period_id, currency_code, exchange_rate, status, created_at, created_by,
    posted_at, posted_by, row_version
)
SELECT gen_random_uuid(), 'GLE', 'R41-' || n, :branch_id,
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
    description
)
SELECT v.id, line_no, :ledger, v.branch_id, v.posting_date, v.period_id,
       CASE WHEN line_no = 1 THEN CAST(:acc_debit AS INTEGER)
            ELSE CAST(:acc_credit AS INTEGER) END,
       'VND', 1,
       CASE WHEN line_no = 1 THEN 100000 ELSE 0 END,
       CASE WHEN line_no = 2 THEN 100000 ELSE 0 END,
       CASE WHEN line_no = 1 THEN 100000 ELSE 0 END,
       CASE WHEN line_no = 2 THEN 100000 ELSE 0 END,
       'perf FR-NFR-041'
FROM (
    SELECT id, branch_id, posting_date, period_id
    FROM vouchers
    WHERE document_type = 'GLE' AND voucher_no LIKE 'R41-%' AND branch_id = :branch_id
) AS v
CROSS JOIN generate_series(1, :lines_per_voucher) AS line_no
"""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def period_window(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> tuple[date, date]:
    """Gieo năm 100.000 chứng từ rồi trả về (đầu, cuối) của kỳ 3."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
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
                "acc_debit": context.accounts["642"],
                "acc_credit": context.accounts["111"],
            },
        )
        row = session.execute(
            text(
                "SELECT start_date, end_date FROM accounting_periods"
                " WHERE fiscal_year_id = :year AND period_no = 3"
            ),
            {"year": context.fiscal_year_id},
        ).one()
        return (row.start_date, row.end_date)


def _range_params(window: tuple[date, date]) -> dict[str, object]:
    return {"from_date": window[0].isoformat(), "to_date": window[1].isoformat()}


def test_xlsx_one_period_on_100k_voucher_year_under_budget(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    period_window: tuple[date, date],
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        started = time.monotonic()
        rendered = render_report(
            session,
            code="S03b-DN",
            output_format="xlsx",
            raw_params=_range_params(period_window),
            today=datetime.now(UTC).astimezone().date(),
        )
        elapsed = time.monotonic() - started
    print(f"\nFR-NFR-041: XLSX Sổ Cái 1 kỳ = {elapsed:.2f}s (mốc < {RENDER_BUDGET_SECONDS}s)")  # noqa: T201 — số đo cho báo cáo phase
    # Kỳ 3 mang ~1/12 của 200.000 dòng — tệp phải THẬT SỰ chứa chúng, một
    # lượt render trả tệp rỗng cũng "đạt mốc" nếu không có phép khẳng định này.
    workbook = openpyxl.load_workbook(io.BytesIO(rendered.content), read_only=True)
    sheet = workbook.active
    assert sheet is not None
    row_count = sum(1 for _ in sheet.iter_rows(values_only=True))
    assert row_count > VOUCHER_COUNT * LINES_PER_VOUCHER // 12 - 1000
    assert elapsed < RENDER_BUDGET_SECONDS, (
        f"XLSX 1 kỳ mất {elapsed:.2f}s (mốc < {RENDER_BUDGET_SECONDS}s)"
    )


def test_preview_one_period_under_budget(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    period_window: tuple[date, date],
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        started = time.monotonic()
        preview = preview_report(session, code="S03b-DN", raw_params=_range_params(period_window))
        elapsed = time.monotonic() - started
    print(f"FR-NFR-041: preview lưới 1 kỳ = {elapsed:.2f}s (mốc < {RENDER_BUDGET_SECONDS}s)")  # noqa: T201 — số đo cho báo cáo phase
    assert preview.truncated, "kỳ ~16.000 dòng phải chạm trần preview 2.000"
    assert elapsed < RENDER_BUDGET_SECONDS


def test_render_estimate_count_is_cheap(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    period_window: tuple[date, date],
) -> None:
    """Lượt COUNT chạy trước MỌI render (ngưỡng chuyển-job) — phải rẻ."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        started = time.monotonic()
        estimated = estimate_report_rows(
            session, code="S03b-DN", raw_params=_range_params(period_window)
        )
        elapsed = time.monotonic() - started
    print(f"FR-NFR-041: COUNT ước lượng = {elapsed:.3f}s (mốc < {COUNT_BUDGET_SECONDS}s)")  # noqa: T201 — số đo cho báo cáo phase
    assert estimated > VOUCHER_COUNT * LINES_PER_VOUCHER // 12 - 1000
    assert elapsed < COUNT_BUDGET_SECONDS
