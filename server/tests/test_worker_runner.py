"""Tiến trình worker chạy trọn một job — từ hàng đợi tới kết quả.

Test hàng đợi kiểm từng câu lệnh; ở đây kiểm **chuỗi**: giành việc dưới danh
nghĩa `ket_worker`, chạy thân job dưới `SET LOCAL ROLE ds_<mã>_app` với phạm vi
chi nhánh đúng, rồi ghi kết quả. Một mắt xích nối sai chỉ lộ ra ở mức này.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.idempotency.models import IdempotencyKey
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import PRUNE_IDEMPOTENCY_KEYS, PRUNE_SESSIONS, SLOW_TASK
from ket.kernel.jobs.models import Job, JobStatus, ResumeSemantics
from ket.kernel.jobs.registry import (
    JobContext,
    JobRegistry,
    JobResult,
    JobType,
)
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import create_session_factory, worker_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.settings import Settings
from ket.worker.runner import Worker

pytestmark = pytest.mark.db

USER_ID = 1


def _scope(dataset: DatasetRef, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=USER_ID, branch_ids=branch_ids)


@pytest.fixture(autouse=True)
def empty_queues(
    drain_jobs: Callable[..., None],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Worker quét **mọi** dataset, nên job sót lại ở beta cũng làm nhiễu test alpha."""
    drain_jobs(dataset_alpha, dataset_beta)


@pytest.fixture
def worker(
    test_settings: Settings,
    worker_session_factory: sessionmaker[Session],
    worker_engine: Engine,
) -> Worker:
    """Worker không có kết nối đặc quyền — đúng cấu hình **mặc định** của bản cài."""
    return Worker(
        test_settings,
        worker_factory=worker_session_factory,
        owner_factory=None,
        engine=worker_engine,
    )


def _wait_for(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    job_id: UUID,
    *,
    status: str,
    branch_ids: tuple[int, ...] = (),
    timeout: timedelta = timedelta(seconds=15),
) -> Job:
    """Chờ job tới một trạng thái, hoặc đỏ với trạng thái thật.

    Có vòng chờ vì một nhánh của test chạy worker ở luồng khác. Thông điệp nêu
    trạng thái quan sát được — "timeout" trần là dòng đỏ vô dụng nhất có thể.
    """
    deadline = datetime.now(UTC) + timeout
    last = ""
    while datetime.now(UTC) < deadline:
        with unit_of_work(session_factory, _scope(dataset, branch_ids)) as session:
            job = queue.get_job(session, job_id)
            last = job.status
            if job.status == status:
                return job
        threading.Event().wait(0.1)
    raise AssertionError(f"job không tới trạng thái {status!r}; trạng thái cuối: {last!r}")


def test_an_empty_queue_makes_one_pass_report_no_work(worker: Worker) -> None:
    assert worker.run_once() is False


def test_the_worker_runs_a_dataset_job_and_records_its_result(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Job dọn khóa idempotency: hình dạng mà gần như mọi job phase sau sẽ có."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.add(
            IdempotencyKey(
                idempotency_key=f"cu-{uuid4().hex[:8]}",
                route_key="POST /api/v1/system/branches",
                user_id=USER_ID,
                request_fingerprint="0" * 64,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        job = queue.enqueue(session, job_type=PRUNE_IDEMPOTENCY_KEYS, requested_by=USER_ID)
        job_id = UUID(str(job.id))

    assert worker.run_once() is True

    done = _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.DONE.value)
    assert done.progress == 100
    assert done.result is not None
    assert isinstance(done.result["removed"], int)
    assert done.result["removed"] >= 1
    assert done.result["dataset_schema"] == dataset_alpha.schema_name


def test_a_job_type_this_binary_does_not_know_fails_with_a_readable_reason(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Đúng kịch bản "worker chạy binary cũ hơn API".

    Nó phải là một job hỏng có lý do, không phải một `KeyError` giết vòng lặp và
    biến mọi job còn lại thành job mồ côi.
    """
    job_id = uuid4()
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.execute(
            text(
                "INSERT INTO jobs (id, type, status, progress, requested_by, "
                "resume_semantics, created_at) VALUES (:id, :type, 'queued', 0, :user, "
                "'idempotent-restart', now())"
            ),
            {"id": job_id, "type": "phase_99.chua_ton_tai", "user": USER_ID},
        )

    worker.run_once()

    failed = _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.FAILED.value)
    assert failed.message is not None
    assert "job.type_unknown" in failed.message


def test_a_privileged_job_is_refused_when_the_worker_has_no_owner_connection(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Mặc định của bản cài: worker **không** cầm quyền `ket_owner`.

    Hướng hỏng đã chọn (xem `JobPrivilege.CONTROL_OWNER`): job dừng với thông
    điệp chỉ đúng hai đường xử lý, chứ worker không âm thầm được nâng quyền để
    xóa được dấu vết đăng nhập.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(session, job_type=PRUNE_SESSIONS, requested_by=USER_ID)
        job_id = UUID(str(job.id))

    worker.run_once()

    failed = _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.FAILED.value)
    assert failed.message is not None
    assert "job.privilege_unavailable" in failed.message


def test_a_privileged_job_runs_when_the_installation_configures_it(
    test_settings: Settings,
    worker_session_factory: sessionmaker[Session],
    worker_engine: Engine,
    owner_engine: Engine,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Nửa còn lại: khai tường minh DSN owner thì job dọn phiên chạy được."""
    privileged = Worker(
        test_settings,
        worker_factory=worker_session_factory,
        owner_factory=create_session_factory(owner_engine),
        engine=worker_engine,
    )
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session,
            job_type=PRUNE_SESSIONS,
            params={"retention_days": 3650},
            requested_by=USER_ID,
        )
        job_id = UUID(str(job.id))

    privileged.run_once()

    done = _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.DONE.value)
    assert done.result is not None
    assert done.result["retention_days"] == 3650


def test_a_job_cancelled_before_it_starts_never_runs(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Hủy trong lúc còn xếp hàng là trường hợp thường gặp nhất.

    Chạy trọn 30 giây một việc đã bị hủy là cách chắc chắn để người dùng không
    tin nút đó nữa.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 600}, requested_by=USER_ID
        )
        job_id = UUID(str(job.id))
        queue.request_cancel(session, job_id)

    worker.run_once()

    cancelled = _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.CANCELLED.value)
    assert cancelled.finished_at is not None


def test_cancelling_a_running_job_stops_it_at_the_next_batch(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Nút Hủy phải dừng được một job **đang chạy**, không chỉ job đang chờ.

    Worker chạy ở luồng khác đúng như tiến trình thật; job kiểm cờ giữa các lô
    nên độ trễ tối đa là một lô (`SLOW_TASK_STEP`).
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 600}, requested_by=USER_ID
        )
        job_id = UUID(str(job.id))

    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.RUNNING.value)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        queue.request_cancel(session, job_id)

    _wait_for(session_factory, dataset_alpha, job_id, status=JobStatus.CANCELLED.value)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_progress_is_visible_while_the_job_is_still_running(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Tiến độ ghi bằng connection **riêng**, ngoài transaction nghiệp vụ.

    Ghi cùng transaction thì thanh tiến độ đứng ở 0% rồi nhảy lên 100% — tức là
    FR-NFR-044 hỏng đúng ở chỗ nó nói về.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 600}, requested_by=USER_ID
        )
        job_id = UUID(str(job.id))

    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=15)
        seen = 0
        while datetime.now(UTC) < deadline and seen == 0:
            with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
                current = queue.get_job(session, job_id)
                seen = current.progress
                assert current.message is not None or seen == 0
            threading.Event().wait(0.1)
        assert seen > 0, "không quan sát được tiến độ nào trong lúc job còn chạy"
    finally:
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            queue.request_cancel(session, job_id)
        thread.join(timeout=10)


class _ProbeParams(BaseModel):
    """Không tham số — job thăm dò chỉ báo cáo ngữ cảnh nó đang chạy trong."""


def test_the_job_body_runs_under_the_dataset_role_and_branch_scope(
    test_settings: Settings,
    worker_session_factory: sessionmaker[Session],
    worker_engine: Engine,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Bất biến trung tâm của quyết định F2, đo trực tiếp trong thân job.

    Giành việc bằng `ket_worker`, nhưng thân job phải chạy dưới `ds_<mã>_app`
    với `ket.branch_ids` đúng bằng chi nhánh của job — nếu không, job sẽ hoặc
    không đọc được gì, hoặc đọc được **mọi** chi nhánh.
    """
    observed: dict[str, str] = {}

    def probe(context: JobContext, _params: _ProbeParams) -> JobResult:
        row = context.session.execute(
            text("SELECT current_user, current_setting('ket.branch_ids', true) AS scope")
        ).one()
        observed["role"] = row.current_user
        observed["scope"] = row.scope
        return None

    registry = JobRegistry()
    probe_type: JobType[Any] = JobType(
        code="test.probe.context",
        permission="system.job.create",
        resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
        params_model=_ProbeParams,
        handler=probe,
    )
    registry.register(probe_type)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        branch = BranchService(session).create(
            code=f"CN_P_{uuid4().hex[:6].upper()}", name="Chi nhánh thăm dò"
        )
        session.flush()
        branch_id = branch.id

    with unit_of_work(session_factory, _scope(dataset_alpha, (branch_id,))) as session:
        job = queue.enqueue(session, job_type=probe_type, requested_by=USER_ID, branch_id=branch_id)
        job_id = UUID(str(job.id))

    probing_worker = Worker(
        test_settings,
        worker_factory=worker_session_factory,
        owner_factory=None,
        engine=worker_engine,
        registry=registry,
    )
    probing_worker.run_once()

    _wait_for(
        session_factory,
        dataset_alpha,
        job_id,
        status=JobStatus.DONE.value,
        branch_ids=(branch_id,),
    )
    assert observed["role"] == f"{dataset_alpha.schema_name}_app"
    assert observed["scope"] == str(branch_id)


def test_one_broken_dataset_does_not_stop_the_others(
    worker: Worker,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Lỗi ở một dữ liệu kế toán không được chặn hàng đợi của những cái còn lại.

    Kịch bản thật: một schema đang khôi phục dở (chưa có bảng `jobs`, chưa cấp
    lại quyền). Không cô lập thì nó chặn mọi dataset đứng sau nó trong danh sách
    — và thứ tự danh sách là thứ không ai kiểm soát.
    """
    with unit_of_work(session_factory, _scope(dataset_beta)) as session:
        job = queue.enqueue(session, job_type=PRUNE_IDEMPOTENCY_KEYS, requested_by=USER_ID)
        job_id = UUID(str(job.id))

    broken = DatasetRef(
        id=-1, code="hong", name="Dataset hỏng", schema_name="ds_khong_ton_tai", scheme="TT99"
    )
    original = worker._datasets

    def datasets_with_a_broken_one() -> tuple[DatasetRef, ...]:
        return (broken, *original())

    worker._datasets = datasets_with_a_broken_one  # type: ignore[method-assign]
    try:
        assert worker.run_once() is True, "dataset lành phía sau vẫn phải chạy được job"
    finally:
        worker._datasets = original  # type: ignore[method-assign]

    _wait_for(session_factory, dataset_beta, job_id, status=JobStatus.DONE.value)


def test_the_loop_survives_a_failure_and_keeps_going(
    worker: Worker, session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`run_forever` không được chết vì một lỗi hạ tầng nhất thời (H2).

    Trên bản cài do người không phải IT vận hành (LD-01), tiến trình worker chết
    im lặng nghĩa là hàng đợi đứng tới khi có ai để ý — triệu chứng đầu tiên là
    "bấm tính giá thành mãi không thấy gì".
    """
    calls = {"n": 0}

    def failing_then_working() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("DB đang khởi động lại"))
        worker.stop()
        return False

    worker._settings = worker._settings.model_copy(update={"worker_error_backoff_seconds": 1})
    worker.run_once = failing_then_working  # type: ignore[method-assign]

    worker.run_forever()

    assert calls["n"] >= 2, "vòng lặp phải chạy tiếp sau lỗi, không thoát"


def test_a_body_that_loses_its_lease_commits_no_business_data(
    test_settings: Settings,
    worker_session_factory: sessionmaker[Session],
    worker_engine: Engine,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Mất lease trong lúc thân job chạy → transaction nghiệp vụ **không commit**.

    Khoảng hở mà `queue.finish` không che được: `finish` có hàng rào nhưng chạy
    *sau* khi `unit_of_work` đã commit — nên một worker bị reaper thu hồi giữa
    chừng vẫn commit trọn dữ liệu nghiệp vụ của mình, song song với lượt chạy
    mới. `Worker._fence_before_commit` gia hạn lease bằng chính session nghiệp
    vụ ngay trước commit; test này dựng đúng chuỗi "job bị giành lại giữa
    chừng" và đòi dữ liệu của lượt cũ biến mất cùng transaction.
    """
    stolen_code = f"CN_FENCE_{uuid4().hex[:6].upper()}"

    def body(context: JobContext, _params: _ProbeParams) -> JobResult:
        BranchService(context.session).create(code=stolen_code, name="Chi nhánh lượt cũ")
        # Giả lập reaper + worker khác: lượt giành mới mang số hiệu mới. Xong
        # việc này, mọi lệnh ghi mang số hiệu cũ phải bị từ chối.
        with worker_session(
            worker_session_factory, dataset_schema=dataset_alpha.schema_name
        ) as thief:
            thief.execute(
                sa_update(Job).where(Job.id == context.job_id).values(attempt=Job.attempt + 1)
            )
        return None

    registry = JobRegistry()
    fence_type: JobType[Any] = JobType(
        code="test.probe.fence",
        permission="system.job.create",
        resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
        params_model=_ProbeParams,
        handler=body,
    )
    registry.register(fence_type)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(session, job_type=fence_type, requested_by=USER_ID)
        job_id = UUID(str(job.id))

    fenced_worker = Worker(
        test_settings,
        worker_factory=worker_session_factory,
        owner_factory=None,
        engine=worker_engine,
        registry=registry,
    )
    fenced_worker.run_once()

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        leaked = session.execute(
            text("SELECT count(*) FROM branches WHERE code = :code"), {"code": stolen_code}
        ).scalar_one()
        status = session.execute(
            text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert leaked == 0, "dữ liệu nghiệp vụ của lượt chạy đã mất lease vẫn được commit"
    assert status != JobStatus.DONE.value, "lượt chạy cũ không được phép kết thúc dòng job"
