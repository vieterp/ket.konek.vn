"""Requeue job mất lease — lưới an toàn cho worker chết giữa chừng (RT-13).

Vấn đề nó giải: worker mất điện, bị `kill -9` lúc nâng cấp, hoặc mất kết nối DB
giữa job. Không ai mở khóa job đó, và nếu chỉ dựa vào worker tự dọn thì dòng
`jobs` kẹt trạng thái "đang chạy" **vĩnh viễn** — người dùng thấy một tác vụ
chạy mãi không xong, và cách sửa duy nhất là sửa DB bằng tay.

Cơ chế: worker đang sống gia hạn `lease_expires_at` theo nhịp heartbeat. Lease
quá hạn = worker đó không còn nữa. Reaper đưa job về `queued` để worker khác
nhận, hoặc đánh hỏng khi job đã thử quá số lần cho phép.

**Trần số lần thử là phần bắt buộc, không phải tùy chọn cho gọn.** Một job làm
worker chết (hết bộ nhớ khi tính lại một năm dữ liệu) sẽ giết luôn worker kế
tiếp; không có trần thì hàng đợi tự nuôi một vòng lặp giết-worker và cả bản cài
mất khả năng chạy bất kỳ job nào khác.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.jobs.models import Job, JobStatus, ResumeSemantics

DEFAULT_MAX_ATTEMPTS: Final[int] = 3
"""Số lần một job được giành trước khi bị đánh hỏng.

3 = một lần chạy bình thường + hai lần cho sự cố hạ tầng thật (khởi động lại
máy chủ giữa job, mạng chớp). Cao hơn thì một job độc hại kéo dài thời gian
hàng đợi tê liệt; thấp hơn (1) thì một lần nâng cấp có kế hoạch cũng làm hỏng
mọi job đang chạy."""


@dataclass(frozen=True)
class ReapOutcome:
    """Kết quả một lượt quét, để worker ghi log có số liệu thay vì "đã chạy"."""

    requeued: int = 0
    failed: int = 0

    def touched(self) -> int:
        return self.requeued + self.failed


def reap_expired_leases(
    session: Session,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> ReapOutcome:
    """Quét job `running` đã hết lease của dataset đang bind và xử lý từng dòng.

    Viết bằng vòng lặp trên tập **đã lọc theo index bộ phận** `ix_jobs_lease`
    chứ không bằng một câu `UPDATE` duy nhất: hai nhánh (requeue / đánh hỏng)
    phụ thuộc `attempt` và `resume_semantics` của từng dòng, và số dòng ở đây
    luôn nhỏ (job hết lease là sự cố, không phải trạng thái thường trực). Đây là
    ngoại lệ hợp lệ của LD-14 — nó không phải đường tính toán khối lượng lớn.

    `FOR UPDATE SKIP LOCKED`: hai worker cùng quét thì mỗi dòng chỉ được một bên
    xử lý, và bên kia đi tiếp thay vì chờ.
    """
    moment = now if now is not None else datetime.now(UTC)
    expired = session.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at < moment,
        )
        .with_for_update(skip_locked=True)
    ).all()

    requeued = 0
    failed = 0
    for job in expired:
        if job.attempt >= max_attempts:
            job.status = JobStatus.FAILED.value
            job.message = (
                f"Tác vụ bị bỏ dở {job.attempt} lần (tiến trình chạy tác vụ dừng giữa chừng). "
                "Kiểm tra nhật ký của tiến trình worker trước khi chạy lại."
            )
            job.finished_at = moment
            job.lease_expires_at = None
            failed += 1
            continue

        job.status = JobStatus.QUEUED.value
        job.started_at = None
        job.heartbeat_at = None
        job.lease_expires_at = None
        job.message = "Tiến trình chạy tác vụ dừng giữa chừng — tác vụ đã được xếp lại hàng đợi."
        # Ngữ nghĩa chạy lại quyết định số phận của tiến độ đã ghi (RT-13):
        # `checkpointed` giữ nguyên `result` để lần chạy sau đi tiếp từ mốc đó,
        # `idempotent-restart` xóa sạch vì nó chạy lại từ đầu và một kết quả cũ
        # còn sót sẽ bị đọc nhầm thành kết quả của lần chạy mới.
        if ResumeSemantics(job.resume_semantics) is ResumeSemantics.IDEMPOTENT_RESTART:
            job.progress = 0
            job.result = None
        requeued += 1

    session.flush()
    return ReapOutcome(requeued=requeued, failed=failed)
