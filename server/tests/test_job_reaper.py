"""Job mồ côi phải quay lại hàng đợi — RT-13.

Kịch bản thật: worker mất điện giữa job. Không ai mở khóa dòng đó, và không có
reaper thì nó kẹt "đang chạy" vĩnh viễn — người dùng thấy một tác vụ chạy mãi
không xong và cách sửa duy nhất là sửa DB bằng tay.

Ở đây "worker chết" mô phỏng bằng cách giành job rồi **đẩy lease về quá khứ**,
đúng trạng thái mà một tiến trình biến mất để lại.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.jobs.models import JobStatus, ResumeSemantics
from ket.kernel.jobs.reaper import DEFAULT_MAX_ATTEMPTS, reap_expired_leases
from ket.kernel.persistence.session import worker_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

USER_ID = 1


@pytest.fixture(autouse=True)
def empty_queue(drain_jobs: Callable[..., None], dataset_alpha: DatasetRef) -> None:
    """Hàng đợi sạch trước mỗi test — xem lý do ở `conftest.drain_jobs`."""
    drain_jobs(dataset_alpha)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=USER_ID, branch_ids=())


def _abandoned_job(
    session_factory: sessionmaker[Session],
    worker_factory: sessionmaker[Session],
    dataset: DatasetRef,
    *,
    attempt: int = 1,
    progress: int = 40,
    result: dict[str, str | int | bool | None] | None = None,
    resume: ResumeSemantics = ResumeSemantics.IDEMPOTENT_RESTART,
) -> UUID:
    """Một job `running` có lease đã hết hạn — dấu vết của worker biến mất."""
    with unit_of_work(session_factory, _scope(dataset)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 1}, requested_by=USER_ID
        )
        job_id = UUID(str(job.id))
        # `resume_semantics` đặt qua vai trò **dataset**: từ khi quyền `UPDATE`
        # của worker bị siết theo cột, worker không đổi được nó — và đó chính là
        # ranh giới đang muốn giữ (ngữ nghĩa chạy lại thuộc về loại job, không
        # thuộc về tiến trình chạy nó).
        job.resume_semantics = resume.value

    with worker_session(worker_factory, dataset_schema=dataset.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        claimed.attempt = attempt
        claimed.progress = progress
        claimed.result = result
    return job_id


def test_an_expired_lease_puts_the_job_back_in_the_queue(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    job_id = _abandoned_job(session_factory, worker_session_factory, dataset_alpha)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        outcome = reap_expired_leases(ws)

    assert outcome.requeued == 1
    assert outcome.failed == 0
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.QUEUED.value
        assert job.lease_expires_at is None
        assert job.started_at is None
        assert job.message is not None, "người vận hành phải đọc được vì sao nó quay lại hàng đợi"


def test_a_job_with_a_live_lease_is_left_alone(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Cướp job của một worker đang khỏe là cách chạy một job hai lần."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 1}, requested_by=USER_ID
        )
        job_id = UUID(str(job.id))

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.claim_next(ws, lease=timedelta(minutes=5))

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert reap_expired_leases(ws).touched() == 0

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).status == JobStatus.RUNNING.value


def test_a_job_that_keeps_killing_the_worker_is_failed_not_requeued_forever(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Trần số lần thử là phần bắt buộc của cơ chế, không phải tùy chọn.

    Một job làm worker chết (hết bộ nhớ khi tính lại một năm dữ liệu) sẽ giết
    luôn worker kế tiếp. Không có trần thì hàng đợi tự nuôi một vòng lặp
    giết-worker và bản cài mất khả năng chạy **mọi** job khác.
    """
    job_id = _abandoned_job(
        session_factory, worker_session_factory, dataset_alpha, attempt=DEFAULT_MAX_ATTEMPTS
    )

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        outcome = reap_expired_leases(ws)

    assert (outcome.requeued, outcome.failed) == (0, 1)
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.finished_at is not None
        assert job.message is not None and str(DEFAULT_MAX_ATTEMPTS) in job.message


def test_requeueing_an_idempotent_restart_job_clears_its_partial_progress(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Job chạy lại **từ đầu** không được mang theo kết quả của lần trước.

    Một `result` còn sót sẽ bị đọc nhầm thành kết quả của lần chạy mới.
    """
    job_id = _abandoned_job(
        session_factory,
        worker_session_factory,
        dataset_alpha,
        progress=70,
        result={"da_lam": 700},
        resume=ResumeSemantics.IDEMPOTENT_RESTART,
    )

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        reap_expired_leases(ws)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.progress == 0
        assert job.result is None


def test_requeueing_a_checkpointed_job_keeps_the_checkpoint(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Ngược lại với trên: `checkpointed` tồn tại đúng để đi tiếp từ mốc cũ.

    Xóa mốc ở đây là biến một job có checkpoint thành một job chạy lại từ đầu —
    tức là ngữ nghĩa mà nó khai không còn nghĩa gì.
    """
    job_id = _abandoned_job(
        session_factory,
        worker_session_factory,
        dataset_alpha,
        progress=70,
        result={"batch": 12},
        resume=ResumeSemantics.CHECKPOINTED,
    )

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        reap_expired_leases(ws)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.QUEUED.value
        assert job.result == {"batch": 12}
        assert job.progress == 70


def test_a_requeued_job_can_be_claimed_again(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Vòng khép kín: reaper trả job về hàng đợi thì worker khác nhận được."""
    job_id = _abandoned_job(session_factory, worker_session_factory, dataset_alpha)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        reap_expired_leases(ws)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws)

        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.attempt == 2, "lần giành thứ hai phải đếm, nếu không trần không bao giờ tới"
