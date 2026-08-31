"""Báo cáo metadata-driven qua HTTP (`/api/v1/reports`) — lát 5C + 5E.

| Đường dẫn | Việc | Quyền |
| --- | --- | --- |
| `GET  /api/v1/reports` | Danh mục báo cáo (lọc category/module) | `reporting.report.view` |
| `GET  /api/v1/reports/{code}/params` | Spec tham số để client dựng form | `reporting.report.view` |
| `POST /api/v1/reports/{code}/preview` | Lưới xem trước (bước 14) | `reporting.report.view` |
| `POST /api/v1/reports/{code}/render` | Kết xuất PDF/XLSX (FR-RPT-006); vượt ngưỡng → `202` + job nền (bước 19) | `reporting.report.export` |
| `GET  /api/v1/reports/render-jobs/{job_id}/file` | Tải tệp của job render đã xong | `reporting.report.export` |

`POST …/render` nằm trong `IDEMPOTENCY_EXEMPT_PREFIXES` (`/api/v1/reports/`,
khai từ phase 2 theo plan.md §Đánh số RT-12): render là phép ĐỌC trả tệp, chạy
hai lần cho cùng một tệp — không có trạng thái nghiệp vụ nào để nhân đôi. Điều
đó đúng cả cho nhánh `202`: bấm hai lần ra hai job cùng đọc một số liệu, tốn
thời gian máy chứ không nhân đôi trạng thái nghiệp vụ nào.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.render_options import build_render_options
from ket.api.routers.reports_schemas import (
    PreviewCellResponse,
    PreviewColumnResponse,
    PreviewRowResponse,
    ReportListResponse,
    ReportParamFieldResponse,
    ReportParamsResponse,
    ReportPreviewRequest,
    ReportPreviewResponse,
    ReportRenderAcceptedResponse,
    ReportRenderRequest,
    ReportSummaryResponse,
)
from ket.kernel.attachments.storage import blob_path
from ket.kernel.config.catalog import (
    REPORT_PDF_JOB_THRESHOLD_KEY,
    REPORT_XLSX_JOB_THRESHOLD_KEY,
)
from ket.kernel.config.reports.models import ReportDefinition
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    AttachmentContentMissingError,
    AttachmentStorageNotConfiguredError,
    JobNotFoundError,
    PermissionDeniedError,
    ReportNotFoundError,
    ReportRenderNotReadyError,
)
from ket.kernel.jobs import queue
from ket.kernel.jobs.models import Job, JobStatus
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import module_view_codes
from ket.reporting.engine import REPORT_EXPORT, REPORT_VIEW
from ket.reporting.engine.engine import (
    estimate_report_rows,
    list_definitions,
    preview_report,
    render_report,
    resolve_definition,
)
from ket.reporting.render_job import RENDER_JOB, RENDER_JOB_CODE

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

ReportViewer = Annotated[AuthorizedRequest, Depends(require_permission(REPORT_VIEW))]
ReportExporter = Annotated[AuthorizedRequest, Depends(require_permission(REPORT_EXPORT))]


def _require_module_access(authorized: AuthorizedRequest, definition: ReportDefinition) -> None:
    """Cổng quyền PHỤ theo phân hệ của báo cáo (review 6E-1 H-1b).

    `reporting.report.view` trả lời "được dùng chức năng báo cáo không", KHÔNG
    trả lời "được đọc dữ liệu phân hệ này không". Trước lát 6E-1 hai câu hỏi đó
    trùng nhau vì mọi báo cáo đều đọc sổ tổng hợp; từ khi báo cáo phân hệ xuất
    hiện thì không còn — `doi-chieu-ngan-hang` đọc `bank_statement_lines`, thứ
    mà `/api/v1/bank/statements` đòi quyền ngân hàng mới cho xem.

    Luật: **có ít nhất một** mã `view` của phân hệ (giống `_may_see_cash` của
    BFF dòng tiền) — người chỉ được cấp quyền xem phiếu thu vẫn đọc được sổ quỹ.
    `required_permission_module IS NULL` = không cổng phụ.
    """
    module = definition.required_permission_module
    if module is None:
        return
    codes = module_view_codes(module)
    if not any(authorized.access.has(code) for code in codes):
        raise PermissionDeniedError(
            f"Tài khoản không có quyền xem dữ liệu phân hệ {module!r}",
            permission=min(codes),
        )


def _require_job_module_access(session: Session, authorized: AuthorizedRequest, job: Job) -> None:
    """Cổng phân hệ cho tệp của một job render, suy từ mã báo cáo trong tham số.

    Tham số job hỏng hoặc mã đã biến mất khỏi danh mục thì coi như không tìm
    thấy tác vụ — cùng câu trả lời với "job của người khác", và không tiết lộ
    rằng có một tệp ở đó.
    """
    code = (job.params or {}).get("code")
    if not isinstance(code, str):  # pragma: no cover - thân job luôn ghi `code`
        raise JobNotFoundError("Không tìm thấy tác vụ", job_id=str(job.id))
    try:
        definition, _spec = resolve_definition(session, code=code)
    except ReportNotFoundError as error:
        raise JobNotFoundError("Không tìm thấy tác vụ", job_id=str(job.id)) from error
    _require_module_access(authorized, definition)


def _may_open(authorized: AuthorizedRequest, definition: ReportDefinition) -> bool:
    """`_require_module_access` ở dạng trả lời thay vì dạng chặn."""
    module = definition.required_permission_module
    if module is None:
        return True
    return any(authorized.access.has(code) for code in module_view_codes(module))


@router.get("", response_model=ReportListResponse)
def get_reports(
    authorized: ReportViewer,
    factory: SessionFactory,
    category: Annotated[str | None, Query(max_length=50)] = None,
    module: Annotated[str | None, Query(max_length=50)] = None,
) -> ReportListResponse:
    """Danh mục báo cáo đã đăng ký trong dữ liệu kế toán này (FR-RPT-001)."""
    with unit_of_work(factory, authorized.scope) as session:
        definitions = list_definitions(session, category=category, module=module)
        # Danh mục chỉ hiện báo cáo người dùng MỞ ĐƯỢC — cùng nguyên tắc
        # `Access.has` ("màn hình chỉ hiện việc người dùng bấm được"). Lọc ở
        # đây không phải cổng an ninh (cổng nằm ở `_require_module_access` của
        # từng cửa); nó chỉ để danh mục không mời người ta bấm vào một 403.
        visible = [item for item in definitions if _may_open(authorized, item)]
        return ReportListResponse(
            reports=[ReportSummaryResponse.model_validate(item) for item in visible]
        )


@router.get("/{code}/params", response_model=ReportParamsResponse)
def get_report_params(
    authorized: ReportViewer,
    factory: SessionFactory,
    code: str,
) -> ReportParamsResponse:
    """Tham số NGOÀI bộ chuẩn của một báo cáo — client dựng form từ đây
    (FR-RPT-002; bộ chuẩn là hợp đồng cố định, xem `reports_schemas`)."""
    with unit_of_work(factory, authorized.scope) as session:
        definition, spec = resolve_definition(session, code=code)
        _require_module_access(authorized, definition)
        # Tham số ĐÃ GHIM không phải ô nhập (review 6E-1 M-2): client dựng form
        # từ đây, và vẽ một ô "Chiều tiền" bắt buộc cho Sổ Nhật ký thu tiền là
        # vẽ một ô mà mọi giá trị khác `thu` đều 422 — giá trị đã nằm trong
        # chính danh tính của mẫu sổ.
        pinned = frozenset(definition.fixed_params)
        # `model_validate` thay vì gọi constructor: `ledger_scope` trong DB là
        # chuỗi tự do, để pydantic tự kiểm nó thuộc đúng bộ Literal của schema.
        return ReportParamsResponse.model_validate(
            {
                "code": definition.code,
                "name": definition.name,
                "ledger_scope": definition.ledger_scope,
                "params": [
                    ReportParamFieldResponse(
                        name=param.name,
                        kind=param.kind,
                        label=param.label,
                        label_en=param.label_en,
                        required=param.required,
                    )
                    for param in spec.params
                    if param.name not in pinned
                ],
            }
        )


@router.post("/{code}/preview", response_model=ReportPreviewResponse)
def preview(
    authorized: ReportViewer,
    factory: SessionFactory,
    settings: AppSettings,
    code: str,
    body: ReportPreviewRequest,
) -> ReportPreviewResponse:
    """Xem trước dạng lưới (bước 14, FR-RPT-001) — quyền `view`, không cần
    `export`: xem một báo cáo trên màn hình và mang được tệp ra ngoài là hai ô
    khác nhau trong ma trận phân quyền. Ô đã định dạng sẵn phía server bằng
    chính pha chữ của bản in; lưới cắt ở trần `PREVIEW_MAX_ROWS` (cờ
    `truncated`) — đường lấy đủ là XLSX."""
    with unit_of_work(factory, authorized.scope) as session:
        options = build_render_options(
            session,
            settings=settings,
            dataset_schema=authorized.scope.dataset_schema,
            user_id=authorized.scope.user_id,
            include_logo=False,
        )
        _require_module_access(authorized, resolve_definition(session, code=code)[0])
        result = preview_report(session, code=code, raw_params=body.params, options=options)
        return ReportPreviewResponse(
            code=result.code,
            name=result.name,
            param_lines=list(result.param_lines),
            columns=[
                PreviewColumnResponse.model_validate(column, from_attributes=True)
                for column in result.layout_spec.columns
            ],
            rows=[
                PreviewRowResponse(
                    kind=row["kind"],
                    heading=row.get("heading"),
                    label_span=row.get("label_span"),
                    cells=(
                        [PreviewCellResponse(**cell) for cell in row["cells"]]
                        if "cells" in row
                        else None
                    ),
                )
                for row in result.rows
            ],
            truncated=result.truncated,
        )


@router.post(
    "/{code}/render",
    # `Response` chứ không `StreamingResponse` — cùng lý do `routers/exports.py`:
    # thân hàm dựng trọn tệp rồi trả một lần; khai streaming ở OpenAPI là nói
    # sai với client sinh type từ đó.
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
            },
            "description": "Tệp báo cáo theo `format` đã chọn",
        },
        202: {
            "model": ReportRenderAcceptedResponse,
            "description": (
                "Báo cáo vượt ngưỡng chuyển-job (bước 19) — đã xếp job nền; "
                "theo dõi qua `/api/v1/jobs/{job_id}`, tải tệp ở "
                "`/api/v1/reports/render-jobs/{job_id}/file`"
            ),
        },
        404: {"description": "Không có báo cáo mang mã này"},
        422: {"description": "Tham số không hợp lệ (FR-RPT-002)"},
    },
)
def render(
    authorized: ReportExporter,
    factory: SessionFactory,
    settings: AppSettings,
    code: str,
    body: ReportRenderRequest,
) -> Response:
    """Kết xuất một báo cáo (FR-RPT-006). Số liệu chạy trong phạm vi RLS của
    người gọi; `branch_ids` chỉ thu hẹp thêm (BR-RPT-04/05).

    Trước khi render, đếm số dòng bằng CHÍNH câu SQL đã bọc phạm vi: vượt
    ngưỡng (`report.{pdf,xlsx}_job_threshold_rows`) thì trả `202` + job nền
    thay vì giữ request nhiều giây (FR-NFR-041/042/044, giải M2 review 5C —
    render đồng bộ giữ transaction + RAM suốt lượt chạy).
    """
    with unit_of_work(factory, authorized.scope) as session:
        _require_module_access(authorized, resolve_definition(session, code=code)[0])
        estimated = estimate_report_rows(session, code=code, raw_params=body.params)
        threshold_key = (
            REPORT_PDF_JOB_THRESHOLD_KEY if body.format == "pdf" else REPORT_XLSX_JOB_THRESHOLD_KEY
        )
        threshold = value_of(session, key=threshold_key, user_id=authorized.scope.user_id)
        if isinstance(threshold, int) and estimated > threshold:
            job = queue.enqueue(
                session,
                job_type=RENDER_JOB,
                params={"code": code, "format": body.format, "params": body.params},
                requested_by=authorized.scope.user_id,
                # Chi nhánh đang thao tác — cùng quyết định với `routers/jobs.py`:
                # nó quyết định phạm vi RLS mà thân job chạy dưới.
                branch_id=authorized.scope.acting_branch_id,
            )
            accepted = ReportRenderAcceptedResponse(job_id=job.id, estimated_rows=estimated)
            return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))
        options = build_render_options(
            session,
            settings=settings,
            dataset_schema=authorized.scope.dataset_schema,
            user_id=authorized.scope.user_id,
        )
        rendered = render_report(
            session,
            code=code,
            output_format=body.format,
            raw_params=body.params,
            options=options,
            # Ngày lập trên khối chữ ký — theo giờ ĐỊA PHƯƠNG của server (bản
            # in tại Việt Nam ghi ngày Việt Nam, không lùi một ngày lúc sáng sớm).
            today=datetime.now(UTC).astimezone().date(),
        )
    return Response(
        content=rendered.content,
        media_type=rendered.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{rendered.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/render-jobs/{job_id}/file",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
            },
            "description": "Tệp kết quả của job render đã `done`",
        },
        404: {"description": "Không có job render này trong phạm vi người gọi"},
        409: {"description": "Job chưa xong (đang chạy / đã hỏng / đã hủy)"},
    },
)
def download_render_job_file(
    authorized: ReportExporter,
    factory: SessionFactory,
    settings: AppSettings,
    job_id: UUID,
) -> Response:
    """Tải tệp của một job `reporting.report.render` đã xong (bước 19).

    Chỉ **người yêu cầu** tải được (404 cho người khác, kể cả người thấy dòng
    job qua RLS chi nhánh): thân job chạy dưới TOÀN BỘ chi nhánh hiện hành của
    người yêu cầu (`JobBranchScope.REQUESTER_BRANCHES`, vá C1 review 5E), nên
    tệp có thể mang số liệu RỘNG hơn phạm vi của một người cùng chi nhánh —
    dòng job nhìn thấy được không có nghĩa tệp đọc được.

    Cổng phân hệ kiểm LẠI ở đây, không mượn lượt kiểm của `/render` (review
    6G-1 M-6): giữa lúc đặt job và lúc tải tệp, quyền có thể đã bị thu hồi —
    và tệp thì vẫn nằm trong kho. "Cổng của bước B không được mượn bộ kiểm của
    bước A" là đúng luật repo, và đây là bước B.
    """
    with unit_of_work(factory, authorized.scope) as session:
        job = queue.get_job(session, job_id)
        if job.type != RENDER_JOB_CODE or job.requested_by != authorized.scope.user_id:
            raise JobNotFoundError("Không tìm thấy tác vụ", job_id=str(job_id))
        _require_job_module_access(session, authorized, job)
        if job.status != JobStatus.DONE.value:
            raise ReportRenderNotReadyError(
                "Tác vụ kết xuất chưa có tệp để tải",
                job_id=str(job_id),
                job_status=job.status,
            )
        result = job.result or {}
        content_hash = result.get("content_hash")
        file_name = result.get("file_name")
        media_type = result.get("media_type")
        if (
            not isinstance(content_hash, str)
            or not isinstance(file_name, str)
            or not isinstance(media_type, str)
        ):  # pragma: no cover - thân job luôn ghi đủ ba khóa khi done
            raise AttachmentContentMissingError(
                "Kết quả của tác vụ kết xuất thiếu tham chiếu tệp", job_id=str(job_id)
            )
        if settings.attachments_dir is None:
            raise AttachmentStorageNotConfiguredError(
                "Bản cài chưa cấu hình thư mục tệp đính kèm (KET_ATTACHMENTS_DIR)"
            )
        try:
            path = blob_path(
                settings.attachments_dir, authorized.scope.dataset_schema, content_hash
            )
        except ValueError as error:
            raise AttachmentContentMissingError(
                "Tệp kết quả không còn trên đĩa", job_id=str(job_id)
            ) from error
        if not path.is_file():
            raise AttachmentContentMissingError(
                "Tệp kết quả không còn trên đĩa", job_id=str(job_id)
            )
    # `FileResponse` stream từ đĩa (M-4 review 5E): trần blob là 512 MiB — đọc
    # trọn vào RAM nghĩa là vài lượt tải song song đủ OOM một bản cài 4 GB.
    return FileResponse(
        path,
        media_type=media_type,
        filename=file_name,
        headers={"X-Content-Type-Options": "nosniff"},
    )
