"""Hàng đợi job trong PostgreSQL — xếp hàng, giành việc, báo tiến độ, kết thúc.

Không thêm Redis vào bản cài (ADR-014, LD-01): `SELECT … FOR UPDATE SKIP LOCKED`
là đúng công cụ cho quy mô mục tiêu (~20 người dùng, ~100.000 chứng từ/năm) và
DB thì đằng nào cũng phải có. Đổi lại, mọi bất biến của hàng đợi — không hai
worker cùng giành một job, không job kẹt "đang chạy" vĩnh viễn — phải viết ra ở
đây chứ không mua sẵn từ một thư viện.

**Hai đường vào, hai vai trò DB khác nhau:**

* Đường API (`enqueue`, `get_job`, `list_jobs`, `request_cancel`) chạy dưới vai
  trò dataset `ds_<mã>_app` trong transaction của request, nên RLS cắt đúng
  phạm vi chi nhánh của người gọi.
* Đường worker (`claim_next`, `heartbeat`, `finish`, `fail`) chạy dưới
  `ket_worker` — vai trò chỉ có quyền trên **đúng bảng `jobs`** và có policy RLS
  riêng cho phép thấy job của mọi chi nhánh (quyết định F2). Worker phải nhìn
  được toàn bộ hàng đợi thì mới chạy được job của chi nhánh mà bản thân nó
  không thuộc về; phạm vi chi nhánh chỉ được đặt lại cho **thân** job.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from ket.kernel.errors import JobNotCancellableError, JobNotFoundError
from ket.kernel.jobs.models import Job, JobStatus, ResumeSemantics
from ket.kernel.jobs.registry import JobResult, JobType
from ket.kernel.persistence.types import AuditValues

DEFAULT_LEASE: Final[timedelta] = timedelta(seconds=60)
"""Một lần giành việc giữ job trong bao lâu nếu worker im lặng (RT-13).

60 giây là bội số thoải mái của nhịp heartbeat mặc định (15 giây): worker phải
lỡ **bốn** nhịp liên tiếp thì reaper mới coi nó đã chết. Ngắn hơn thì một lần
GC dài hoặc một câu truy vấn nặng cũng đủ làm job bị cướp khỏi worker đang chạy
tốt — và hai worker cùng chạy một job phân bổ khấu hao là thứ tệ hơn hẳn việc
chờ thêm một phút."""

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {JobStatus.DONE.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
)

MAX_MESSAGE_LENGTH: Final[int] = 500
"""Bằng độ rộng cột `jobs.message`. Cắt ở đây thay vì để `INSERT` đổ: thông điệp
dài nhất đến từ chính chỗ tệ nhất — một traceback rút gọn của job vừa hỏng."""


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _clip(message: str | None) -> str | None:
    if message is None:
        return None
    return message[:MAX_MESSAGE_LENGTH]


def enqueue(
    session: Session,
    *,
    job_type: JobType[Any],
    params: Mapping[str, object] | None = None,
    requested_by: int,
    branch_id: int | None = None,
    now: datetime | None = None,
) -> Job:
    """Xếp một job vào hàng đợi của dataset đang bind.

    Tham số được **ép kiểu ngay tại đây** (`parse_params`) rồi ghi lại dạng đã
    chuẩn hóa: sai tham số hỏng ở lượt bấm nút, chứ không phải sau vài phút nằm
    trong hàng đợi ở một tiến trình khác mà người dùng không nhìn thấy.

    `resume_semantics` chép từ registry vào **dòng job** chứ không tra lại lúc
    reaper chạy: reaper phải quyết định được số phận của một job kể cả khi loại
    job đó vừa bị gỡ khỏi binary đang chạy.
    """
    parsed = job_type.parse_params(params)
    job = Job(
        id=uuid4(),
        type=job_type.code,
        params=dict(parsed.model_dump(mode="json")),
        status=JobStatus.QUEUED.value,
        progress=0,
        branch_id=branch_id,
        requested_by=requested_by,
        created_at=_now(now),
        resume_semantics=job_type.resume_semantics.value,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: UUID) -> Job:
    """Một job trong dataset đang bind.

    Không thấy dòng có hai nghĩa — không tồn tại, hoặc thuộc chi nhánh ngoài
    phạm vi người gọi (RLS lọc trước khi mã này chạy) — và cả hai đều trả 404
    có chủ đích: phân biệt chúng là nói cho người ngoài phạm vi biết job đó có
    tồn tại.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError("Không tìm thấy tác vụ", job_id=str(job_id))
    return job


def list_jobs(
    session: Session,
    *,
    statuses: Sequence[str] | None = None,
    limit: int = 50,
) -> tuple[Job, ...]:
    """Job của dataset đang bind, mới nhất trước."""
    statement = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if statuses:
        statement = statement.where(Job.status.in_(tuple(statuses)))
    return tuple(session.scalars(statement).all())


def request_cancel(session: Session, job_id: UUID) -> Job:
    """Đặt cờ hủy. Worker dừng ở **ranh giới lô** gần nhất, không bị giết giữa chừng.

    Giết một job đang ghi bút toán phân bổ giữa chừng là cách tạo ra sổ lệch,
    nên hủy phải là yêu cầu chứ không phải lệnh. Job đã kết thúc thì không có
    nơi nhận yêu cầu đó — trả `409` thay vì gật đầu im lặng.

    **Khóa dòng trước khi kiểm** (`FOR UPDATE`): không khóa thì đây là khuôn
    đọc-kiểm-rồi-ghi kinh điển, và khoảng hở đo được là — request đọc job lúc
    `running`, worker `finish` và commit, request đặt cờ hủy lên một job đã
    `done`. Client nhận `200` cho một việc không xảy ra, và dòng job mang một cờ
    hủy vô nghĩa mà màn hình sẽ hiển thị.
    """
    job = session.scalars(select(Job).where(Job.id == job_id).with_for_update()).one_or_none()
    if job is None:
        raise JobNotFoundError("Không tìm thấy tác vụ", job_id=str(job_id))
    if job.status in TERMINAL_STATUSES:
        raise JobNotCancellableError(
            "Tác vụ đã kết thúc nên không hủy được nữa",
            job_id=str(job_id),
            status=job.status,
        )
    job.cancel_requested = True
    session.flush()
    return job


def claim_next(
    session: Session,
    *,
    lease: timedelta = DEFAULT_LEASE,
    now: datetime | None = None,
) -> Job | None:
    """Giành job chờ cũ nhất của dataset đang bind, hoặc `None` nếu hàng đợi rỗng.

    `FOR UPDATE SKIP LOCKED` là phần không thay thế được: nhiều worker chạy song
    song thì hai tiến trình phải **không bao giờ** giành cùng một dòng, mà cũng
    không được xếp hàng chờ nhau — `SKIP LOCKED` cho worker thứ hai bỏ qua dòng
    đang bị khóa và lấy dòng kế tiếp.

    `attempt` tăng **ngay lúc giành**, không phải lúc hỏng: một job làm worker
    chết (hết bộ nhớ, lỗi phân đoạn) sẽ không bao giờ chạy tới nhánh ghi lỗi, và
    nếu chỉ đếm ở đó thì reaper requeue nó vô hạn — mỗi lần lại giết một worker.
    """
    moment = _now(now)
    job = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).one_or_none()
    if job is None:
        return None

    job.status = JobStatus.RUNNING.value
    job.started_at = moment
    job.heartbeat_at = moment
    job.lease_expires_at = moment + lease
    job.attempt += 1
    job.message = None
    session.flush()
    return job


def heartbeat(
    session: Session,
    job_id: UUID,
    *,
    attempt: int,
    lease: timedelta = DEFAULT_LEASE,
    progress: int | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Gia hạn lease (và ghi tiến độ). Trả `False` nếu job không còn là của mình.

    `attempt` là **hàng rào** (fence token), không phải thông tin trang trí — xem
    `_fenced_update`.
    """
    moment = _now(now)
    values: dict[str, object] = {"heartbeat_at": moment, "lease_expires_at": moment + lease}
    if progress is not None:
        values["progress"] = max(0, min(100, progress))
    if message is not None:
        values["message"] = _clip(message)
    return _fenced_update(session, job_id, attempt=attempt, values=values)


def finish(
    session: Session,
    job_id: UUID,
    *,
    attempt: int,
    result: JobResult = None,
    now: datetime | None = None,
) -> bool:
    """Đánh dấu xong, ghi kết quả, nhả lease. `False` nếu lượt chạy này đã mất lease."""
    return _fenced_update(
        session,
        job_id,
        attempt=attempt,
        values={
            "status": JobStatus.DONE.value,
            "progress": 100,
            "result": result,
            "finished_at": _now(now),
            "lease_expires_at": None,
        },
    )


def fail(
    session: Session,
    job_id: UUID,
    *,
    attempt: int,
    message: str,
    now: datetime | None = None,
) -> bool:
    """Đánh dấu hỏng kèm lý do người vận hành đọc được.

    **Không** requeue ở đây: một job hỏng vì dữ liệu sai sẽ hỏng lại y hệt ở lần
    thử thứ hai, và thử lại tự động chỉ làm nhật ký dài ra. Requeue là việc của
    reaper và chỉ cho đúng một nguyên nhân — worker biến mất giữa chừng (RT-13).
    """
    return _fenced_update(
        session,
        job_id,
        attempt=attempt,
        values={
            "status": JobStatus.FAILED.value,
            "message": _clip(message),
            "finished_at": _now(now),
            "lease_expires_at": None,
        },
    )


def mark_cancelled(
    session: Session, job_id: UUID, *, attempt: int, now: datetime | None = None
) -> bool:
    """Kết thúc job vì người dùng đã bấm Hủy và thân job đã dừng ở ranh giới lô."""
    return _fenced_update(
        session,
        job_id,
        attempt=attempt,
        values={
            "status": JobStatus.CANCELLED.value,
            "finished_at": _now(now),
            "lease_expires_at": None,
        },
    )


def _fenced_update(
    session: Session, job_id: UUID, *, attempt: int, values: dict[str, object]
) -> bool:
    """Ghi trạng thái job **chỉ khi** lượt chạy này còn giữ lease. `False` nếu không.

    Ba điều kiện trong **một** câu lệnh, và mất bất kỳ điều nào là mất cả cơ chế:

    * `id = :job_id` — dòng cần ghi;
    * `status = 'running'` — job chưa kết thúc. Thiếu nó, một job đã `cancelled`
      bị `finish` biến thành `done`: người dùng thấy "đã hủy" rồi vài giây sau
      thấy "hoàn thành", và `finished_at` bị ghi đè nên không còn dấu vết;
    * `attempt = :attempt` — **hàng rào**. `attempt` tăng ở mỗi lượt giành
      (`claim_next`) nên nó là số hiệu lượt chạy. Worker bị reaper thu hồi giữa
      chừng mang số hiệu cũ; khi worker khác đã giành lại, mọi lệnh ghi của
      worker cũ khớp `id`, khớp `status`, nhưng **không** khớp số hiệu — nên
      chúng không ghi được dòng nào.

    Kịch bản mà hàng rào này chặn đã đo được, không phải giả định: A giành →
    lease hết hạn → reaper requeue → B giành → A vẫn `heartbeat` thành công rồi
    `finish` đè kết quả của mình lên lượt chạy của B (hai lượt cùng commit vào
    một dòng job).

    `UPDATE … WHERE` chứ không đọc-rồi-ghi bằng ORM: điều kiện phải do PostgreSQL
    phân xử trong cùng câu lệnh với lệnh ghi. Đọc trước, kiểm ở Python, rồi ghi
    là đúng khoảng hở mà hai tiến trình chen vào được.
    """
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.RUNNING.value,
            Job.attempt == attempt,
        )
        .values(**values)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


def cancel_requested(session: Session, job_id: UUID) -> bool:
    """Cờ hủy đọc lại **từ DB**, không từ đối tượng đã nạp.

    Yêu cầu hủy đến từ một transaction khác (request HTTP của người dùng) sau
    khi worker đã nạp dòng job, nên đọc thuộc tính trên đối tượng cũ sẽ mãi mãi
    trả `False` — nút Hủy bấm được mà không dừng được gì.
    """
    job = session.get(Job, job_id)
    if job is None:
        return False
    session.refresh(job, attribute_names=["cancel_requested"])
    return job.cancel_requested


def checkpoint(session: Session, job_id: UUID, *, attempt: int, state: AuditValues) -> bool:
    """Ghi mốc tiến độ cho job khai `checkpointed` (RT-13).

    Ghi vào chính cột `result`: sau khi job xong, cột này mang kết quả cuối; khi
    job còn dở, nó mang chỗ đứng để lần chạy sau đi tiếp. Một cột cho hai nghĩa
    là có chủ đích — trạng thái nào cũng đọc kèm `status`, và thêm một cột chỉ
    để phân biệt hai pha của cùng một dữ liệu là thêm một thứ nữa để lệch.
    """
    return _fenced_update(session, job_id, attempt=attempt, values={"result": state})


def resume_semantics_of(job: Job) -> ResumeSemantics:
    """Ngữ nghĩa chạy lại đã đông cứng trên dòng job (không tra lại registry)."""
    return ResumeSemantics(job.resume_semantics)
