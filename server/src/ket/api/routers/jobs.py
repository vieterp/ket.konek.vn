"""Hàng đợi tác vụ nền qua HTTP (`/api/v1/jobs`) — FR-NFR-042/044.

Bốn việc: xem bản cài này chạy được tác vụ gì, xếp một tác vụ vào hàng, theo dõi
tiến độ, và yêu cầu hủy. Tiến trình chạy tác vụ nằm ngoài (`python -m ket.worker`)
— tầng API **không bao giờ** chạy thân job, kể cả job ngắn: đó là ranh giới giữ
cho FR-NFR-044 ("giao diện không bị khóa") đúng, và `import-linter` canh nó
(`api` không được import `worker`).

**Miễn trừ idempotency (FR-NFR-004, RT-12), khai sẵn từ lát trước:** xếp hàng là
`POST` nhưng không đổi trạng thái nghiệp vụ — nó tạo một *yêu cầu*, và bấm hai
lần chỉ tạo hai lượt chạy của cùng một việc vốn phải chịu được chạy lại
(`resume_semantics`). Bắt khóa ở đây chỉ thêm nghi thức.

**Hai lớp quyền cho một lượt xếp hàng**, và hai lớp trả lời hai câu khác nhau:
`system.job.create` = được dùng hàng đợi hay không; `JobType.permission` = được
chạy *việc này* hay không. Không có lớp thứ hai thì bất kỳ ai xếp được hàng cũng
chạy được tác vụ dọn dẹp — trong khi từ phase 4 hàng đợi sẽ mang cả tính giá
thành lẫn khóa sổ.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.jobs_schemas import (
    JobEnqueueRequest,
    JobListResponse,
    JobResponse,
    JobTypeListResponse,
    JobTypeResponse,
)
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import JOB_CANCEL, JOB_CREATE, JOB_VIEW
from ket.kernel.jobs.models import JobStatus
from ket.kernel.jobs.registry import REGISTRY, JobPrivilege
from ket.kernel.persistence.unit_of_work import unit_of_work

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

JobViewer = Annotated[AuthorizedRequest, Depends(require_permission(JOB_VIEW))]

MAX_PAGE_SIZE = 200


@router.get("/types", response_model=JobTypeListResponse)
def list_job_types(authorized: JobViewer) -> JobTypeListResponse:
    """Loại tác vụ mà **bản cài này** biết chạy.

    Lọc theo quyền của người gọi: danh sách trả về là những việc họ bấm được,
    nên màn hình không hiện một nút chắc chắn trả `403`.
    """
    return JobTypeListResponse(
        items=[
            JobTypeResponse(
                code=job_type.code,
                permission=job_type.permission,
                resume_semantics=job_type.resume_semantics.value,
                description=job_type.description,
                requires_privileged_connection=(job_type.privilege is JobPrivilege.CONTROL_OWNER),
            )
            for job_type in REGISTRY.types()
            if authorized.access.has(job_type.permission)
        ]
    )


@router.post("", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_job(
    payload: JobEnqueueRequest,
    authorized: Annotated[AuthorizedRequest, Depends(require_permission(JOB_CREATE))],
    factory: SessionFactory,
) -> JobResponse:
    """Xếp một tác vụ vào hàng đợi của dữ liệu kế toán đang mở.

    `202` chứ không `201`: yêu cầu đã được ghi nhận, kết quả chưa có. Client
    theo dõi bằng `GET /api/v1/jobs/{id}`.

    Quyền riêng của loại tác vụ kiểm **trong thân hàm** vì nó phụ thuộc thân
    request; dependency ở chữ ký giữ mức sàn, nên người không có quyền dùng hàng
    đợi dừng lại từ trước.

    Chi nhánh của job = chi nhánh **đang thao tác** của người yêu cầu. Đó là thứ
    quyết định phạm vi RLS mà thân job chạy dưới, nên nó phải là chi nhánh của
    người bấm nút chứ không phải một tham số client gửi lên.
    """
    job_type = REGISTRY.get(payload.type)
    authorized.access.require(job_type.permission)

    with unit_of_work(factory, authorized.scope) as session:
        job = queue.enqueue(
            session,
            job_type=job_type,
            params=payload.params,
            requested_by=authorized.scope.user_id,
            branch_id=authorized.scope.acting_branch_id,
        )
        return JobResponse.of(job)


@router.get("", response_model=JobListResponse)
def list_jobs(
    authorized: JobViewer,
    factory: SessionFactory,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> JobListResponse:
    """Hàng đợi của dữ liệu kế toán đang mở, mới nhất trước.

    RLS lọc theo chi nhánh trước khi mã này chạy, nên "mọi job" ở đây nghĩa là
    mọi job **người gọi được thấy**.

    Bộ lọc trạng thái khai bằng `JobStatus` để FastAPI trả `422` cho giá trị lạ.
    Nhận `str` trần thì `?status=runing` cho ra danh sách rỗng — client đọc
    thành "hàng đợi trống" và không có gì báo là đã gõ sai.
    """
    statuses = [job_status.value] if job_status is not None else None

    with unit_of_work(factory, authorized.scope) as session:
        jobs = queue.list_jobs(session, statuses=statuses, limit=limit)
        return JobListResponse(items=[JobResponse.of(job) for job in jobs])


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: UUID, authorized: JobViewer, factory: SessionFactory) -> JobResponse:
    """Trạng thái + tiến độ của một tác vụ."""
    with unit_of_work(factory, authorized.scope) as session:
        return JobResponse.of(queue.get_job(session, job_id))


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: UUID,
    authorized: Annotated[AuthorizedRequest, Depends(require_permission(JOB_CANCEL))],
    factory: SessionFactory,
) -> JobResponse:
    """Yêu cầu dừng một tác vụ.

    Đặt cờ chứ không giết tiến trình: worker dừng ở **ranh giới lô** gần nhất.
    Giết giữa một bút toán phân bổ là cách tạo ra sổ lệch, nên phản hồi ở đây là
    "đã ghi nhận", không phải "đã dừng" — trạng thái thật đọc ở lần `GET` sau.
    """
    with unit_of_work(factory, authorized.scope) as session:
        return JobResponse.of(queue.request_cancel(session, job_id))
