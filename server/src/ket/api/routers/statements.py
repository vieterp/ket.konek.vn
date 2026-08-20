"""BCTC qua HTTP (`/api/v1/statements`) — lát 5B mang danh sách mẫu + xem trước.

Đọc-only (luật phụ thuộc #4: `reporting` chỉ đọc): mọi con số suy ra từ
`gl_postings`/`opening_balances` qua statement builder. RLS lọc chi nhánh trước
khi mã này chạy; `branch_id` chỉ thu hẹp thêm trong phạm vi đã thấy. Kết xuất
PDF/XLSX + màn hình nhóm "Sổ sách & Thuế" vào ở lát 5C–5E.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.statements_schemas import (
    StatementLayoutListResponse,
    StatementLayoutSummaryResponse,
    StatementPreviewResponse,
    StatementRowValueResponse,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.engine.models import Ledger
from ket.reporting.statements import STATEMENT_VIEW
from ket.reporting.statements.builder import build_statement, list_layouts

router = APIRouter(prefix="/api/v1/statements", tags=["statements"])

StatementViewer = Annotated[AuthorizedRequest, Depends(require_permission(STATEMENT_VIEW))]


@router.get("", response_model=StatementLayoutListResponse)
def get_statement_layouts(
    authorized: StatementViewer,
    factory: SessionFactory,
    period_id: Annotated[int, Query(ge=1)],
) -> StatementLayoutListResponse:
    """Mọi mẫu BCTC của gói cấu hình hiệu lực cho kỳ đã chọn.

    Nhận `period_id` chứ không nhận mã gói: gói nào hiệu lực là chuyện của
    `resolve_package` (chế độ kế toán của năm + ngày cuối kỳ), client không
    được chọn tay để khỏi xem nhầm bộ mẫu của chế độ khác (FR-NFR-070).
    """
    with unit_of_work(factory, authorized.scope) as session:
        package_code, layouts = list_layouts(session, period_id=period_id)
        return StatementLayoutListResponse(
            package_code=package_code,
            layouts=[StatementLayoutSummaryResponse.model_validate(layout) for layout in layouts],
        )


@router.get("/{layout_code}/preview", response_model=StatementPreviewResponse)
def preview_statement(
    authorized: StatementViewer,
    factory: SessionFactory,
    layout_code: str,
    period_id: Annotated[int, Query(ge=1)],
    ledger: Annotated[int, Query(ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)] = Ledger.FINANCIAL,
    branch_id: Annotated[int | None, Query(ge=1)] = None,
) -> StatementPreviewResponse:
    """Lập một BCTC và trả dạng lưới để client xem trước (FR-GLE-043).

    Đổi `ledger` cho hai bộ số độc lập trên cùng mẫu (BR-RPT-04); bỏ trống
    `branch_id` là BCTC gộp mọi chi nhánh trong phạm vi người gọi thấy.
    """
    with unit_of_work(factory, authorized.scope) as session:
        result = build_statement(
            session,
            layout_code=layout_code,
            period_id=period_id,
            ledger=ledger,
            branch_id=branch_id,
        )
        return StatementPreviewResponse(
            layout_code=result.layout_code,
            name=result.name,
            name_en=result.name_en,
            statement_kind=result.statement_kind,
            package_code=result.package_code,
            ledger=result.ledger,
            period_id=result.period_id,
            branch_id=result.branch_id,
            rows=[StatementRowValueResponse.model_validate(row) for row in result.rows],
        )
