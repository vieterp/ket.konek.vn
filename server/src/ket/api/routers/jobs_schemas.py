"""Hợp đồng HTTP của hàng đợi tác vụ nền. Pydantic ở mọi ranh giới (ADR-015)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from ket.kernel.jobs.models import Job


class JobEnqueueRequest(BaseModel):
    """Yêu cầu xếp một tác vụ vào hàng đợi."""

    type: str = Field(min_length=1, max_length=100)
    """Mã loại tác vụ, ví dụ `system.maintenance.prune_idempotency_keys`."""

    params: dict[str, str | int | bool | None] | None = None
    """Tham số, ép về model của loại tác vụ ngay tại endpoint.

    Kiểu giá trị hẹp (không `Any`) theo luật phụ thuộc #7: tham số job đi qua
    JSONB và qua hàng đợi, nên nó phải là dữ liệu phẳng — cấu trúc lồng nhau là
    dấu hiệu tham số đang gánh việc của một bảng."""


class JobResponse(BaseModel):
    """Trạng thái một tác vụ, đủ để màn hình vẽ thanh tiến độ."""

    id: UUID
    type: str
    status: str
    progress: int
    message: str | None
    result: dict[str, str | int | bool | None] | None
    branch_id: int | None
    requested_by: int
    attempt: int
    cancel_requested: bool
    resume_semantics: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, job: Job) -> JobResponse:
        return cls(
            id=job.id,
            type=job.type,
            status=job.status,
            progress=job.progress,
            message=job.message,
            result=job.result,
            branch_id=job.branch_id,
            requested_by=job.requested_by,
            attempt=job.attempt,
            cancel_requested=job.cancel_requested,
            resume_semantics=job.resume_semantics,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


class JobListResponse(BaseModel):
    items: list[JobResponse]


class JobTypeResponse(BaseModel):
    """Một loại tác vụ mà bản cài này biết chạy.

    Lộ ra để màn hình dựng được danh sách "chạy tác vụ" từ đúng một lượt gọi,
    thay vì mang một bản sao của registry trong client — bản sao sẽ lệch ngay ở
    phase sau, khi mỗi phân hệ thêm loại của mình.
    """

    code: str
    permission: str
    resume_semantics: str
    description: str
    requires_privileged_connection: bool


class JobTypeListResponse(BaseModel):
    items: list[JobTypeResponse]
