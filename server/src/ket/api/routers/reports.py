"""Báo cáo metadata-driven qua HTTP (`/api/v1/reports`) — lát 5C.

| Đường dẫn | Việc | Quyền |
| --- | --- | --- |
| `GET  /api/v1/reports` | Danh mục báo cáo (lọc category/module) | `reporting.report.view` |
| `GET  /api/v1/reports/{code}/params` | Spec tham số để client dựng form | `reporting.report.view` |
| `POST /api/v1/reports/{code}/render` | Kết xuất PDF/XLSX (FR-RPT-006) | `reporting.report.export` |

`POST …/render` nằm trong `IDEMPOTENCY_EXEMPT_PREFIXES` (`/api/v1/reports/`,
khai từ phase 2 theo plan.md §Đánh số RT-12): render là phép ĐỌC trả tệp, chạy
hai lần cho cùng một tệp — không có trạng thái nghiệp vụ nào để nhân đôi.

Đồng bộ, không qua hàng đợi — cùng lý lẽ với `routers/exports.py`; ngưỡng
chuyển báo cáo lớn (>20.000 dòng ước lượng) sang job queue là bước 19 của
phase-05, thuộc lát 5E.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.reports_schemas import (
    ReportListResponse,
    ReportParamFieldResponse,
    ReportParamsResponse,
    ReportRenderRequest,
    ReportSummaryResponse,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.reporting.engine import REPORT_EXPORT, REPORT_VIEW
from ket.reporting.engine.engine import list_definitions, render_report, resolve_definition

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

ReportViewer = Annotated[AuthorizedRequest, Depends(require_permission(REPORT_VIEW))]
ReportExporter = Annotated[AuthorizedRequest, Depends(require_permission(REPORT_EXPORT))]


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
        return ReportListResponse(
            reports=[ReportSummaryResponse.model_validate(item) for item in definitions]
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
                ],
            }
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
        404: {"description": "Không có báo cáo mang mã này"},
        422: {"description": "Tham số không hợp lệ (FR-RPT-002)"},
    },
)
def render(
    authorized: ReportExporter,
    factory: SessionFactory,
    code: str,
    body: ReportRenderRequest,
) -> Response:
    """Kết xuất một báo cáo (FR-RPT-006). Số liệu chạy trong phạm vi RLS của
    người gọi; `branch_ids` chỉ thu hẹp thêm (BR-RPT-04/05)."""
    with unit_of_work(factory, authorized.scope) as session:
        rendered = render_report(
            session,
            code=code,
            output_format=body.format,
            raw_params=body.params,
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
