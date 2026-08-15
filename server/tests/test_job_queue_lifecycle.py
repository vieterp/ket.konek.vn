"""Vòng đời một tác vụ nền: xếp hàng → giành việc → tiến độ → kết thúc.

Chạy trên PostgreSQL thật vì thứ đang kiểm **là** hành vi của PostgreSQL:
`FOR UPDATE SKIP LOCKED` không mô phỏng được, và hai worker giành cùng một job
là tình huống chỉ tồn tại khi có khóa hàng thật.

Đường giành việc dùng `worker_session_factory` (vai trò `ket_worker`), đường xếp
hàng dùng `session_factory` (vai trò dataset) — đúng như hai tiến trình thật.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import JobNotCancellableError, JobNotFoundError, JobParamsInvalidError
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import PRUNE_IDEMPOTENCY_KEYS, SLOW_TASK
from ket.kernel.jobs.models import JobStatus
from ket.kernel.persistence.session import worker_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

WORKER_USER_ID = 1


@pytest.fixture(autouse=True)
def empty_queues(
    drain_jobs: Callable[..., None],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Xóa sạch hàng đợi trước **mỗi** test.

    Không phải dọn dẹp cho gọn: `claim_next` lấy job **cũ nhất**, nên một job
    còn sót từ test trước sẽ được giành thay cho job mà test hiện tại vừa xếp —
    và test đỏ ở một chỗ không liên quan gì tới thứ nó đang kiểm. Dòng job không
    phải dữ liệu kế toán và không có nhật ký nào trỏ vào nó, nên xóa được.
    """
    drain_jobs(dataset_alpha, dataset_beta)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=WORKER_USER_ID, branch_ids=())


def _claim(worker_factory: sessionmaker[Session], dataset: DatasetRef) -> int:
    """Giành job kế tiếp, trả **số hiệu lượt chạy** (hàng rào cho lệnh ghi sau đó)."""
    with worker_session(worker_factory, dataset_schema=dataset.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        return claimed.attempt


def _enqueue(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    *,
    seconds: int = 1,
) -> str:
    with unit_of_work(session_factory, _scope(dataset)) as session:
        job = queue.enqueue(
            session,
            job_type=SLOW_TASK,
            params={"seconds": seconds},
            requested_by=WORKER_USER_ID,
        )
        return str(job.id)


def test_a_queued_job_carries_the_resume_semantics_of_its_type(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ngữ nghĩa chạy lại đông cứng trên **dòng job**, không tra lại registry.

    Reaper phải quyết định được số phận của một job kể cả khi loại job đó vừa bị
    gỡ khỏi binary đang chạy.
    """
    job_id = _enqueue(session_factory, dataset_alpha)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, UUID(job_id))
        assert job.status == JobStatus.QUEUED.value
        assert job.resume_semantics == SLOW_TASK.resume_semantics.value
        assert job.attempt == 0
        assert job.params == {"seconds": 1}


def test_enqueue_refuses_params_that_do_not_match_the_declared_model(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tham số sai hỏng ở lượt bấm nút, không phải sau vài phút trong hàng đợi."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(JobParamsInvalidError) as exc:
            queue.enqueue(
                session,
                job_type=SLOW_TASK,
                params={"seconds": 9_999},
                requested_by=WORKER_USER_ID,
            )
    assert exc.value.details["field"] == "seconds"


def test_claiming_moves_the_job_to_running_and_takes_a_lease(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    job_id = UUID(_enqueue(session_factory, dataset_alpha))

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws, lease=timedelta(seconds=30))
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING.value
        assert claimed.lease_expires_at is not None
        assert claimed.attempt == 1, "attempt tăng ngay lúc giành, không phải lúc hỏng"

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).status == JobStatus.RUNNING.value


def test_two_workers_never_claim_the_same_job(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Đây là lý do `FOR UPDATE SKIP LOCKED` tồn tại.

    Hai transaction mở **cùng lúc**: nếu bỏ `SKIP LOCKED` thì bên thứ hai chờ
    rồi giành lại đúng dòng đó sau khi bên đầu commit — hai worker chạy một job,
    và với một job phân bổ khấu hao thì đó là một bảng bị nhân đôi.
    """
    first_id = UUID(_enqueue(session_factory, dataset_alpha))
    second_id = UUID(_enqueue(session_factory, dataset_alpha))

    with (
        worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as one,
        worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as two,
    ):
        claimed_one = queue.claim_next(one)
        claimed_two = queue.claim_next(two)

        assert claimed_one is not None and claimed_two is not None
        assert claimed_one.id != claimed_two.id
        assert {claimed_one.id, claimed_two.id} == {first_id, second_id}


def test_an_empty_queue_returns_none_instead_of_blocking(
    worker_session_factory: sessionmaker[Session], dataset_beta: DatasetRef
) -> None:
    with worker_session(worker_session_factory, dataset_schema=dataset_beta.schema_name) as ws:
        assert queue.claim_next(ws) is None


def test_heartbeat_extends_the_lease_and_records_progress(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws, lease=timedelta(seconds=10))
        assert claimed is not None and claimed.id == job_id
        first_deadline = claimed.lease_expires_at
        attempt = claimed.attempt

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        alive = queue.heartbeat(
            ws, job_id, attempt=attempt, lease=timedelta(seconds=60), progress=42, message="nửa"
        )
        assert alive is True

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.progress == 42
        assert job.message == "nửa"
        assert first_deadline is not None and job.lease_expires_at is not None
        assert job.lease_expires_at > first_deadline


def test_heartbeat_on_a_job_that_is_no_longer_running_reports_the_lease_is_gone(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Worker cũ phải **dừng lại**, không ghi đè việc của worker mới.

    Đây là nhánh mà `heartbeat` trả `False` thay vì im lặng — nếu nó im lặng,
    một worker bị reaper thu hồi vẫn ghi tiếp kết quả lên dòng job đã thuộc về
    người khác.
    """
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        attempt = claimed.attempt
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.finish(ws, job_id, attempt=attempt, result={"xong": True}) is True

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.heartbeat(ws, job_id, attempt=attempt) is False


def test_finishing_writes_the_result_and_releases_the_lease(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.finish(ws, job_id, attempt=attempt, result={"removed": 3})

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.DONE.value
        assert job.progress == 100
        assert job.result == {"removed": 3}
        assert job.lease_expires_at is None
        assert job.finished_at is not None


def test_a_long_failure_message_is_clipped_instead_of_breaking_the_write(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Thông điệp dài nhất đến từ chỗ tệ nhất: một job vừa hỏng.

    Không cắt thì `INSERT` đổ vì tràn cột, và job hỏng biến thành job **mồ côi**
    — đúng trạng thái mà nhánh ghi lỗi sinh ra để tránh.
    """
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.fail(ws, job_id, attempt=attempt, message="x" * 5_000)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.message is not None
        assert len(job.message) == queue.MAX_MESSAGE_LENGTH


def test_cancelling_a_finished_job_is_a_conflict_not_a_silent_yes(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.finish(ws, job_id, attempt=attempt)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(JobNotCancellableError):
            queue.request_cancel(session, job_id)


def test_the_cancel_flag_is_read_back_from_the_database(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Yêu cầu hủy đến từ **transaction khác** sau khi worker đã nạp dòng job.

    Đọc thuộc tính trên đối tượng cũ sẽ mãi mãi trả `False`, tức là nút Hủy bấm
    được mà không dừng được gì.
    """
    job_id = UUID(_enqueue(session_factory, dataset_alpha, seconds=60))
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.claim_next(ws)
        assert queue.cancel_requested(ws, job_id) is False

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        queue.request_cancel(session, job_id)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.cancel_requested(ws, job_id) is True


def test_a_job_from_another_dataset_is_not_visible(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Hàng đợi cũng là dữ liệu của **một** doanh nghiệp (ADR-017)."""
    job_id = UUID(_enqueue(session_factory, dataset_alpha))

    with unit_of_work(session_factory, _scope(dataset_beta)) as session:
        with pytest.raises(JobNotFoundError):
            queue.get_job(session, job_id)


def test_listing_filters_by_status_and_returns_newest_first(
    session_factory: sessionmaker[Session], dataset_beta: DatasetRef
) -> None:
    older = UUID(_enqueue(session_factory, dataset_beta))
    newer = UUID(_enqueue(session_factory, dataset_beta))

    with unit_of_work(session_factory, _scope(dataset_beta)) as session:
        queued = queue.list_jobs(session, statuses=[JobStatus.QUEUED.value])
        ids = [job.id for job in queued]
        assert ids.index(newer) < ids.index(older)
        assert queue.list_jobs(session, statuses=[JobStatus.DONE.value]) == ()


def test_checkpoint_survives_for_a_job_that_declares_it(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Mốc chạy tiếp ghi vào chính cột `result` (xem `queue.checkpoint`)."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(session, job_type=PRUNE_IDEMPOTENCY_KEYS, requested_by=WORKER_USER_ID)
        job_id = UUID(str(job.id))

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        queue.checkpoint(ws, job_id, attempt=claimed.attempt, state={"batch": 7})

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).result == {"batch": 7}


def test_created_at_ordering_decides_who_runs_first(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_beta: DatasetRef,
) -> None:
    """Hàng đợi là hàng đợi: ai xếp trước chạy trước.

    Không có ưu tiên ở phase này — thêm cột ưu tiên khi có một job thật cần nó,
    chứ không phải trước đó (YAGNI).
    """
    with unit_of_work(session_factory, _scope(dataset_beta)) as session:
        first = queue.enqueue(
            session,
            job_type=SLOW_TASK,
            params={"seconds": 1},
            requested_by=WORKER_USER_ID,
            now=datetime.now(UTC) - timedelta(minutes=5),
        )
        first_id = UUID(str(first.id))
        queue.enqueue(session, job_type=SLOW_TASK, params={"seconds": 1}, requested_by=1)

    with worker_session(worker_session_factory, dataset_schema=dataset_beta.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None and claimed.id == first_id


def test_claiming_skips_jobs_that_are_not_queued(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """`claim_next` chỉ giành job **đang chờ** — và đó phải có test riêng.

    Kiểm đột biến của review cho thấy: gỡ bộ lọc `status = 'queued'` khỏi
    `claim_next` **sống sót toàn bộ bộ test**, vì mỗi test khác dọn hàng đợi rồi
    xếp đúng một job. Với đột biến đó, worker giành lại cả job đã xong (dòng cũ
    nhất) và chạy lại chúng — tức là chạy lại một bút toán đã ghi.

    Ở đây job đã kết thúc được xếp **trước**, nên nó là dòng cũ nhất: nếu bộ lọc
    biến mất, `claim_next` sẽ trả nó chứ không trả job đang chờ.
    """
    finished_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.finish(ws, finished_id, attempt=attempt) is True

    waiting_id = UUID(_enqueue(session_factory, dataset_alpha))

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        claimed = queue.claim_next(ws)
        assert claimed is not None
        assert claimed.id == waiting_id, "job đã kết thúc không được giành lại"


def test_a_queue_with_only_finished_jobs_looks_empty(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Nửa còn lại của cùng bất biến: hết việc nghĩa là hết việc."""
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        queue.finish(ws, job_id, attempt=attempt)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.claim_next(ws) is None


def test_progress_outside_the_allowed_range_is_clamped(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Kẹp 0–100 là thứ đứng giữa một lỗi làm tròn và một ràng buộc DB đổ.

    `jobs.progress` có `CHECK (progress BETWEEN 0 AND 100)`; mất kẹp thì một
    phép chia lệch biến job **đang chạy tốt** thành job hỏng. Đột biến gỡ kẹp
    từng sống sót toàn bộ bộ test.
    """
    job_id = UUID(_enqueue(session_factory, dataset_alpha))
    attempt = _claim(worker_session_factory, dataset_alpha)

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.heartbeat(ws, job_id, attempt=attempt, progress=150) is True
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).progress == 100

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.heartbeat(ws, job_id, attempt=attempt, progress=-5) is True
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert queue.get_job(session, job_id).progress == 0
