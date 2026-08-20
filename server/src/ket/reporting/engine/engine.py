"""Điều phối một lượt render: metadata → tham số → dòng → renderer (FR-RPT-001).

Đọc-only (luật phụ thuộc #4). Mọi mảnh đều là dữ liệu: definition trỏ dataset/
layout/param set; engine không biết "Sổ Cái" là gì — thêm một báo cáo mới là
chèn dữ liệu, không sửa tệp này (tiêu chí phase-05: thêm báo cáo lúc server
đang chạy).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.reports.models import (
    ReportDataset,
    ReportDefinition,
    ReportLayout,
    ReportParamSet,
)
from ket.kernel.config.reports.spec import (
    LayoutSpec,
    ParamSetSpec,
    parse_layout_spec,
    parse_param_set_spec,
)
from ket.kernel.errors import ReportNotFoundError
from ket.kernel.excel.template import XLSX_MEDIA_TYPE
from ket.kernel.security.models import Branch
from ket.reporting.engine.executor import execute_dataset
from ket.reporting.engine.grouping import group_rows
from ket.reporting.engine.params import BoundParams, Format, validate_params
from ket.reporting.rendering.header import load_unit_info
from ket.reporting.rendering.pdf_renderer import render_pdf
from ket.reporting.rendering.xlsx_renderer import render_xlsx

PDF_MEDIA_TYPE = "application/pdf"


@dataclass(frozen=True)
class RenderedReport:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class _ResolvedReport:
    definition: ReportDefinition
    dataset: ReportDataset
    layout_spec: LayoutSpec
    param_set_spec: ParamSetSpec


def list_definitions(
    session: Session, *, category: str | None = None, module: str | None = None
) -> tuple[ReportDefinition, ...]:
    """Danh mục báo cáo (màn *Sổ sách & Thuế*, lát 5E) — lọc tùy chọn."""
    statement = select(ReportDefinition).order_by(ReportDefinition.category, ReportDefinition.code)
    if category is not None:
        statement = statement.where(ReportDefinition.category == category)
    if module is not None:
        statement = statement.where(ReportDefinition.module == module)
    return tuple(session.execute(statement).scalars().all())


def resolve_definition(session: Session, *, code: str) -> tuple[ReportDefinition, ParamSetSpec]:
    """Definition + spec tham số (cho client dựng form) — 404 khi mã lạ."""
    resolved = _resolve(session, code=code)
    return resolved.definition, resolved.param_set_spec


def render_report(
    session: Session,
    *,
    code: str,
    output_format: Format,
    raw_params: Mapping[str, object],
    today: date,
) -> RenderedReport:
    """Một lượt kết xuất trọn vẹn: kiểm tham số → chạy SQL → nhóm → PDF/XLSX."""
    resolved = _resolve(session, code=code)
    bound = validate_params(
        raw_params,
        spec=resolved.param_set_spec,
        ledger_scope=resolved.definition.ledger_scope,
    )
    param_lines = _param_lines_with_branches(session, bound)
    rows = execute_dataset(
        session,
        dataset=resolved.dataset,
        layout_spec=resolved.layout_spec,
        binds=bound.sql_binds(resolved.dataset.allowed_params),
    )
    display_rows = group_rows(rows, layout_spec=resolved.layout_spec)
    unit = load_unit_info(session)
    title = resolved.definition.name

    if output_format == "pdf":
        content = render_pdf(
            title=title,
            unit=unit,
            param_lines=param_lines,
            layout_spec=resolved.layout_spec,
            display_rows=display_rows,
            signature_date=today,
        )
        media_type = PDF_MEDIA_TYPE
    elif output_format == "xlsx":
        content = render_xlsx(
            title=title,
            unit=unit,
            param_lines=param_lines,
            layout_spec=resolved.layout_spec,
            display_rows=display_rows,
        )
        media_type = XLSX_MEDIA_TYPE
    else:
        assert_never(output_format)

    filename = (
        f"{resolved.definition.code}-{bound.from_date:%Y%m%d}-{bound.to_date:%Y%m%d}"
        f".{output_format}"
    )
    return RenderedReport(content=content, media_type=media_type, filename=filename)


def _resolve(session: Session, *, code: str) -> _ResolvedReport:
    definition = session.execute(
        select(ReportDefinition).where(ReportDefinition.code == code)
    ).scalar_one_or_none()
    if definition is None:
        raise ReportNotFoundError("Không có báo cáo mang mã này", report_code=code)
    dataset = session.get(ReportDataset, definition.dataset_code)
    layout = session.get(ReportLayout, definition.layout_code)
    param_set = session.get(ReportParamSet, definition.param_set_code)
    if dataset is None or layout is None or param_set is None:  # pragma: no cover - FK bảo đảm
        raise RuntimeError(f"Báo cáo {code!r} trỏ mảnh ghép không tồn tại")
    # Parse lại tại ranh giới render dù seed/import đã kiểm — cùng lý do
    # statement builder parse lại công thức: một dòng bị sửa tay trong DB phải
    # nổ thành lỗi cấu hình rõ ràng, không thành bản in sai lặng lẽ.
    layout_spec = parse_layout_spec(layout.spec, layout_code=layout.code)
    param_set_spec = parse_param_set_spec(param_set.spec, param_set_code=param_set.code)
    return _ResolvedReport(
        definition=definition,
        dataset=dataset,
        layout_spec=layout_spec,
        param_set_spec=param_set_spec,
    )


def _param_lines_with_branches(session: Session, bound: BoundParams) -> tuple[str, ...]:
    """Dòng tham số cho tiêu đề (BR-RPT-03) — đổi id chi nhánh thành TÊN.

    Không truyền `branch_ids` = báo cáo gộp mọi chi nhánh trong phạm vi người
    gọi — ghi rõ như vậy thay vì im lặng, vì hai người khác phạm vi in cùng
    tham số sẽ ra hai bản số khác nhau (RLS), và bản in phải tự nói điều đó.
    """
    if bound.branch_ids is None:
        return (*bound.echo_lines, "Chi nhánh: toàn bộ trong phạm vi người lập")
    names = session.execute(
        select(Branch.name).where(Branch.id.in_(bound.branch_ids)).order_by(Branch.path)
    ).scalars()
    return (*bound.echo_lines, f"Chi nhánh: {', '.join(names)}")
