"""Một dòng job chỉ nhận lệnh ghi của **lượt chạy đang giữ lease** (RT-13).

Lát 2B-2b ban đầu chỉ chặn hai worker **giành** cùng một job. Review thù địch đo
được lỗ còn lại và nó nghiêm trọng hơn: hai worker **chạy** cùng một job. Chuỗi
sự kiện có thật, không phải giả định —

    A giành → lease hết hạn (A treo/GC/mất mạng) → reaper requeue → B giành
    → A tỉnh lại, `heartbeat` **thành công**, rồi `finish` ghi kết quả của mình

Kết quả đo được lúc đó: job `done` mang kết quả của A trong khi B còn đang chạy.
Với một job phân bổ khấu hao ở phase 8 thì đó là hai lượt ghi sổ cùng commit.

Hàng rào: `attempt` (số hiệu lượt giành) đi vào mệnh đề `WHERE` của **mọi** lệnh
ghi trạng thái. Test ở đây dựng lại đúng chuỗi trên và đòi lượt chạy cũ bị từ
chối ở từng đường một.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.jobs.models import JobStatus
from ket.kernel.jobs.reaper import reap_expired_leases
from ket.kernel.persistence.session import worker_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

USER_ID = 1


@pytest.fixture(autouse=True)
def empty_queue(drain_jobs: Callable[..., None], dataset_alpha: DatasetRef) -> None:
    drain_jobs(dataset_alpha)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=USER_ID, branch_ids=())


def _queued(session_factory: sessionmaker[Session], dataset: DatasetRef) -> UUID:
    with unit_of_work(session_factory, _scope(dataset)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 1}, requested_by=USER_ID
        )
        return UUID(str(job.id))


def _claim(worker_factory: sessionmaker[Session], dataset: DatasetRef) -> int:
    """Giành job kế tiếp, trả **số hiệu lượt chạy** (fence token)."""
    with worker_session(worker_factory, dataset_schema=dataset.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        return claimed.attempt


def _expire_lease(worker_factory: sessionmaker[Session], dataset: DatasetRef, job_id: UUID) -> None:
    """Đẩy lease về quá khứ — dấu vết mà một worker biến mất để lại."""
    with worker_session(worker_factory, dataset_schema=dataset.schema_name) as ws:
        job = queue.get_job(ws, job_id)
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)


def _steal(worker_factory: sessionmaker[Session], dataset: DatasetRef, job_id: UUID) -> int:
    """Diễn lại "reaper thu hồi rồi worker khác giành lại". Trả lượt chạy mới."""
    _expire_lease(worker_factory, dataset, job_id)
    with worker_session(worker_factory, dataset_schema=dataset.schema_name) as ws:
        assert reap_expired_leases(ws).requeued == 1
    return _claim(worker_factory, dataset)


def test_the_previous_run_cannot_extend_a_lease_it_already_lost(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """`heartbeat` của lượt chạy cũ phải trả `False` — đó là tín hiệu dừng của nó."""
    job_id = _queued(session_factory, dataset_alpha)
    old_attempt = _claim(worker_session_factory, dataset_alpha)
    new_attempt = _steal(worker_session_factory, dataset_alpha, job_id)

    assert new_attempt > old_attempt
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.heartbeat(ws, job_id, attempt=old_attempt, progress=99) is False
        assert queue.heartbeat(ws, job_id, attempt=new_attempt, progress=10) is True

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).progress == 10, "tiến độ của lượt chạy mới"


def test_the_previous_run_cannot_finish_a_job_that_was_taken_over(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Đây là kịch bản đắt nhất: hai lượt chạy cùng ghi kết quả vào một dòng job."""
    job_id = _queued(session_factory, dataset_alpha)
    old_attempt = _claim(worker_session_factory, dataset_alpha)
    new_attempt = _steal(worker_session_factory, dataset_alpha, job_id)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.finish(ws, job_id, attempt=old_attempt, result={"nguon": "luot cu"}) is False

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.RUNNING.value, "lượt chạy mới vẫn đang chạy"
        assert job.result is None

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.finish(ws, job_id, attempt=new_attempt, result={"nguon": "luot moi"}) is True

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).result == {"nguon": "luot moi"}


def test_the_previous_run_cannot_fail_or_cancel_a_job_that_was_taken_over(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Cả hai đường kết thúc còn lại đều phải chịu cùng hàng rào.

    Bịt một đường mà quên hai đường kia thì lượt chạy cũ vẫn đóng được job của
    lượt mới — chỉ đổi trạng thái nó ghi vào.
    """
    job_id = _queued(session_factory, dataset_alpha)
    old_attempt = _claim(worker_session_factory, dataset_alpha)
    _steal(worker_session_factory, dataset_alpha, job_id)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.fail(ws, job_id, attempt=old_attempt, message="lượt cũ") is False
        assert queue.mark_cancelled(ws, job_id, attempt=old_attempt) is False
        assert queue.checkpoint(ws, job_id, attempt=old_attempt, state={"moc": 1}) is False

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.RUNNING.value
        assert job.result is None
        assert job.message is None


@pytest.mark.parametrize(
    ("write", "label"),
    [
        (lambda ws, job_id, attempt: queue.finish(ws, job_id, attempt=attempt), "finish"),
        (
            lambda ws, job_id, attempt: queue.fail(ws, job_id, attempt=attempt, message="x"),
            "fail",
        ),
        (lambda ws, job_id, attempt: queue.mark_cancelled(ws, job_id, attempt=attempt), "cancel"),
    ],
)
def test_a_finished_job_cannot_be_resurrected(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    write: Callable[[Session, UUID, int], bool],
    label: str,
) -> None:
    """Job đã kết thúc thì mọi lệnh ghi trạng thái đều không ăn.

    Đo được trước khi có `status = 'running'` trong mệnh đề `WHERE`: một job đã
    `cancelled` bị `finish` biến thành `done` — người dùng thấy "đã hủy" rồi vài
    giây sau thấy "hoàn thành", và `finished_at` bị ghi đè nên không còn dấu vết
    nào của lần hủy.
    """
    job_id = _queued(session_factory, dataset_alpha)
    attempt = _claim(worker_session_factory, dataset_alpha)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.mark_cancelled(ws, job_id, attempt=attempt) is True
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        finished_at = queue.get_job(session, job_id).finished_at

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert write(ws, job_id, attempt) is False, label

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.CANCELLED.value
        assert job.finished_at == finished_at, "dấu vết lần hủy không được ghi đè"


def test_a_write_for_a_job_that_no_longer_exists_reports_failure(
    worker_session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Không ném `JobNotFoundError` ở đường worker: nó chỉ cần biết "không ăn".

    Job bị xóa (khôi phục dữ liệu, dọn dẹp bằng tay) trong lúc worker chạy là
    chuyện hiếm nhưng có thật, và nó không đáng làm cả vòng lặp đổ.
    """
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.heartbeat(ws, uuid4(), attempt=1) is False
        assert queue.finish(ws, uuid4(), attempt=1) is False
