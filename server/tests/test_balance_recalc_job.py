"""Vỏ job `posting.balances.recalc` — hợp đồng với hàng đợi tác vụ nền (4B).

Phần lõi tính lại đã có `test_balance_recalc.py`; ở đây kiểm ba thứ chỉ tồn
tại ở lớp job:

* tham số + phạm vi: job không chi nhánh bị từ chối sớm; ép tính lại phải nêu
  đủ cặp `(ledger, from_period_id)`;
* hủy giữa chừng ném `JobCancelled` ở ranh giới kỳ (rollback do worker lo);
* đăng ký: loại job phải nhìn thấy được từ tiến trình worker THẬT
  (`python -m ket.worker`) — không chỉ trong test, nơi conftest đã import
  `ket.model_registry` hộ.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.registry import REGISTRY, JobCancelled, JobContext
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.recalc_job import BalanceRecalcParams, run_balance_recalc
from posting_support import PostingContext, posting_scope, seed_posting_context

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)


def test_recalc_job_type_is_registered() -> None:
    job_type = REGISTRY.get("posting.balances.recalc")
    assert job_type.permission == "posting.balance.create"


def test_worker_entrypoint_sees_module_job_types() -> None:
    """Worker thật phải biết job của module — không chỉ ba loại builtin.

    Chạy một tiến trình Python MỚI import đúng thứ `python -m ket.worker`
    import: trong tiến trình test thì `conftest` đã kéo `ket.model_registry`
    vào từ lâu, nên chỉ subprocess mới đo được điều này. Trước 4B worker thiếu
    dòng import đó và mọi job `master.import.*` sẽ hỏng với "không biết loại
    tác vụ" ngay trên bản cài thật.
    """
    code = (
        "import ket.worker.__main__\n"
        "from ket.kernel.jobs.registry import REGISTRY\n"
        "codes = set(REGISTRY.codes())\n"
        "assert 'posting.balances.recalc' in codes, codes\n"
        "assert 'master.import.validate' in codes, codes\n"
    )
    result = subprocess.run(  # noqa: S603 — chạy chính interpreter của test, đối số cố định
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _context_for(
    session: Session,
    dataset: DatasetRef,
    *,
    branch_id: int | None,
    progress: FakeProgress | None = None,
) -> JobContext:
    return JobContext(
        job_id=uuid4(),
        session=session,
        progress=progress if progress is not None else FakeProgress(reports=[]),
        attempt=1,
        dataset_schema=dataset.schema_name,
        branch_id=branch_id,
        requested_by=ACTOR_ID,
    )


def test_forced_recalc_requires_both_ledger_and_period() -> None:
    # Phép kiểm cặp tham số đứng TRƯỚC mọi lượt chạm session, nên context ở
    # đây mang session giả — tới được DB là test này phải đổ.
    fake_dataset = DatasetRef(id=0, code="x", schema_name="ds_x", scheme="TT99")
    with pytest.raises(JobParamsInvalidError):
        run_balance_recalc(
            _context_for(None, fake_dataset, branch_id=1),  # type: ignore[arg-type]
            BalanceRecalcParams(ledger=0),
        )


@pytest.mark.db
class TestWithDatabase:
    @pytest.fixture
    def context(
        self, session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
    ) -> PostingContext:
        return seed_posting_context(session_factory, dataset_alpha)

    @pytest.fixture
    def run(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> Callable[[Callable[[Session], object]], object]:
        def runner(work: Callable[[Session], object]) -> object:
            scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
            with unit_of_work(session_factory, scope) as session:
                return work(session)

        return runner

    def _post_one(self, session: Session, context: PostingContext) -> None:
        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chứng từ test job",
                lines=(
                    JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(10_000)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(10_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

    def test_job_without_branch_is_refused(
        self,
        run: Callable[[Callable[[Session], object]], object],
        dataset_alpha: DatasetRef,
    ) -> None:
        def work(session: Session) -> object:
            with pytest.raises(JobParamsInvalidError):
                run_balance_recalc(
                    _context_for(session, dataset_alpha, branch_id=None),
                    BalanceRecalcParams(),
                )
            return None

        run(work)

    def test_job_reports_progress_and_clears_queue(
        self,
        run: Callable[[Callable[[Session], object]], object],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        def work(session: Session) -> object:
            self._post_one(session, context)
            progress = FakeProgress(reports=[])
            result = run_balance_recalc(
                _context_for(
                    session, dataset_alpha, branch_id=context.branch_id, progress=progress
                ),
                BalanceRecalcParams(),
            )
            assert result is not None
            assert result["periods_recalced"] == 24
            assert result["marks_cleared"] == 2
            assert progress.reports[-1][0] == 100
            return None

        run(work)

    def test_job_with_empty_queue_reports_nothing_to_do(
        self,
        run: Callable[[Callable[[Session], object]], object],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        def work(session: Session) -> object:
            result = run_balance_recalc(
                _context_for(session, dataset_alpha, branch_id=context.branch_id),
                BalanceRecalcParams(),
            )
            assert result is not None
            assert result["periods_recalced"] == 0
            return None

        run(work)

    def test_job_serializes_per_branch_with_an_advisory_lock(
        self,
        run: Callable[[Callable[[Session], object]], object],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        """Hai job recalc song song cùng chi nhánh phải XẾP HÀNG, không chết.

        Không có khóa advisory, job thua chết `duplicate key
        uq_account_balances_key` (probe review 4B, M-A). Khóa là xact-scope nên
        kiểm ngay trong transaction của thân job: `pg_locks` phải có một dòng
        advisory của chính backend này sau khi thân job chạy.
        """

        def work(session: Session) -> object:
            self._post_one(session, context)
            run_balance_recalc(
                _context_for(session, dataset_alpha, branch_id=context.branch_id),
                BalanceRecalcParams(),
            )
            held = session.execute(
                text(
                    "SELECT COUNT(*) FROM pg_locks"
                    " WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                )
            ).scalar_one()
            assert held == 1
            return None

        run(work)

    def test_cancel_stops_at_period_boundary(
        self,
        run: Callable[[Callable[[Session], object]], object],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        def work(session: Session) -> object:
            self._post_one(session, context)
            progress = FakeProgress(reports=[], cancelled=True)
            with pytest.raises(JobCancelled):
                run_balance_recalc(
                    _context_for(
                        session, dataset_alpha, branch_id=context.branch_id, progress=progress
                    ),
                    BalanceRecalcParams(),
                )
            return None

        run(work)
