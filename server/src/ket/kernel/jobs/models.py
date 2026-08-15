"""Hàng đợi job nặng — bảng trong PostgreSQL, không thêm Redis (ADR-014).

Bản cài mục tiêu là LAN hoặc **một máy do người không phải IT cài** (LD-01).
Mỗi thành phần phải cài thêm là một điểm hỏng nữa ở nơi không có ai trực. DB
đã bắt buộc phải có, và `SELECT … FOR UPDATE SKIP LOCKED` đủ cho quy mô mục
tiêu (~20 người dùng, ~100.000 chứng từ/năm — OQ#3 đã chốt).

Ba cột chống job mồ côi (RT-13): `lease_expires_at`, `heartbeat_at`, `attempt`.
Worker chết vì mất điện thì không có ai mở khóa job; không có lease thì job đó
kẹt "đang chạy" vĩnh viễn và người dùng không có cách nào ngoài sửa DB tay.
Logic reaper + worker thuộc bước 11 (slice sau) — bảng phải có ở `0001` vì
`ALTER TABLE` trên bảng đang chạy job là việc không ai muốn làm lúc nâng cấp.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.types import AuditValues


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResumeSemantics(StrEnum):
    """Chạy lại một job dở dang thì phải làm gì (RT-13).

    Mỗi loại job **bắt buộc** khai giá trị này lúc đăng ký. Không có mặc định:
    reaper cần biết chạy lại từ đầu có an toàn không, và câu trả lời "chắc là
    được" đã đủ để nhân đôi một bảng phân bổ khấu hao.
    """

    IDEMPOTENT_RESTART = "idempotent-restart"
    CHECKPOINTED = "checkpointed"


class Job(DatasetBase):
    """Một lần chạy tác vụ nền (FR-NFR-042/044).

    Cố ý **không** `Audited`: job đổi trạng thái liên tục theo cơ chế nội bộ và
    ghi vết chúng chỉ làm loãng nhật ký kiểm toán. Việc *nghiệp vụ* mà job thực
    hiện thì vẫn ghi vết bình thường, trong chính transaction của nó.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in JobStatus) + ")",
            name="status_known",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
        # Hai index bộ phận cho hai truy vấn nóng: worker lấy job chờ (cũ nhất
        # trước), reaper quét job đang chạy đã hết lease. Bộ phận chứ không
        # toàn phần vì job đã xong chiếm gần hết bảng và không truy vấn nào
        # trong hai đường này cần tới chúng.
        Index("ix_jobs_queue", "created_at", postgresql_where=text("status = 'queued'")),
        Index(
            "ix_jobs_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    params: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=JobStatus.QUEUED.value,
        server_default=JobStatus.QUEUED.value,
    )
    progress: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)

    branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Hủy là **yêu cầu**, không phải lệnh giết: worker kiểm giữa các lô để dừng
    ở ranh giới nhất quán. Giết giữa chừng một bút toán phân bổ là cách tạo ra
    sổ lệch."""

    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    resume_semantics: Mapped[str] = mapped_column(String(30), nullable=False)
