"""Điều phối một lượt render: metadata → tham số → dòng → renderer (FR-RPT-001).

Đọc-only (luật phụ thuộc #4). Mọi mảnh đều là dữ liệu: definition trỏ dataset/
layout/param set; engine không biết "Sổ Cái" là gì — thêm một báo cáo mới là
chèn dữ liệu, không sửa tệp này (tiêu chí phase-05: thêm báo cáo lúc server
đang chạy).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import assert_never

from sqlalchemy import ColumnElement, or_, select, text
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ConfigPackage
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
from ket.kernel.datasets.models import Dataset
from ket.kernel.errors import ReportNotFoundError
from ket.kernel.excel.template import XLSX_MEDIA_TYPE
from ket.kernel.security.models import Branch
from ket.reporting.engine.executor import count_dataset_rows, execute_dataset
from ket.reporting.engine.grouping import group_rows
from ket.reporting.engine.params import BoundParams, Format, validate_params
from ket.reporting.rendering.header import load_unit_info
from ket.reporting.rendering.options import DEFAULT_RENDER_OPTIONS, RenderOptions
from ket.reporting.rendering.pdf_renderer import render_pdf
from ket.reporting.rendering.presentation import PresentationRow, presentation_rows
from ket.reporting.rendering.xlsx_renderer import render_xlsx

PDF_MEDIA_TYPE = "application/pdf"

PREVIEW_MAX_ROWS = 2000
"""Trần dòng trình bày của một bản xem trước (bước 14).

Preview là lưới NHÌN trên màn hình — đổ 50.000 dòng JSON vào một response vừa
nghẽn client vừa vô nghĩa với người đọc; đường lấy đủ dữ liệu là XLSX (spike
S2: 50k dòng ≈ 1,1s). Cắt ở dòng TRÌNH BÀY chứ không dòng dữ liệu để nhóm
đang mở không mất tiêu đề."""


@dataclass(frozen=True)
class RenderedReport:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class ReportPreview:
    """Bản xem trước dạng lưới — ô đã qua đúng pha chữ của bản in."""

    code: str
    name: str
    param_lines: tuple[str, ...]
    layout_spec: LayoutSpec
    rows: tuple[PresentationRow, ...]
    truncated: bool


@dataclass(frozen=True)
class _ResolvedReport:
    definition: ReportDefinition
    dataset: ReportDataset
    layout_spec: LayoutSpec
    param_set_spec: ParamSetSpec


def _scheme_condition(session: Session) -> ColumnElement[bool]:
    """Điều kiện "báo cáo thuộc chế độ kế toán của dữ liệu này" (review 5D, H2).

    `package_id` được GHI lúc seed thì phải có đường ĐỌC — không lọc thì mọi
    dataset phục vụ cả 8 mã mẫu của CẢ HAI thông tư: doanh nghiệp TT99 render
    được `F01-DNN`, tờ in mang mã mẫu + tiêu đề của chế độ kế toán khác (cùng
    họ khuôn với CRITICAL của 5A: cột kích hoạt ghi mà resolve không đọc).

    Scheme lấy từ dòng đăng ký dataset (`public.datasets` — nằm trên
    `search_path`, vai trò dataset kế thừa quyền đọc qua `ket_control`): v1 mỗi
    dữ liệu kế toán chạy MỘT chế độ (LD-06 — TT99/TT133 là lựa chọn lúc tạo);
    nếu về sau một dataset đổi chế độ theo niên độ thì chỗ này đổi sang lọc
    theo năm chứa `from_date`, cùng cách `statements/list_layouts` lọc theo kỳ.
    `package_id IS NULL` = báo cáo không phụ thuộc chế độ — luôn phục vụ.
    """
    schema = session.scalar(text("SELECT current_schema()"))
    scheme = session.execute(
        select(Dataset.scheme).where(Dataset.schema_name == schema)
    ).scalar_one_or_none()
    if scheme is None:  # pragma: no cover - dataset nào cũng có dòng đăng ký
        return ReportDefinition.package_id.is_(None)
    return or_(
        ReportDefinition.package_id.is_(None),
        ReportDefinition.package_id.in_(
            select(ConfigPackage.id).where(ConfigPackage.scheme == scheme)
        ),
    )


def list_definitions(
    session: Session, *, category: str | None = None, module: str | None = None
) -> tuple[ReportDefinition, ...]:
    """Danh mục báo cáo (màn *Sổ sách & Thuế*, lát 5E) — chỉ báo cáo thuộc chế
    độ kế toán đang dùng (H2) + lọc tùy chọn."""
    statement = (
        select(ReportDefinition)
        .where(_scheme_condition(session))
        .order_by(ReportDefinition.category, ReportDefinition.code)
    )
    if category is not None:
        statement = statement.where(ReportDefinition.category == category)
    if module is not None:
        statement = statement.where(ReportDefinition.module == module)
    return tuple(session.execute(statement).scalars().all())


def resolve_definition(session: Session, *, code: str) -> tuple[ReportDefinition, ParamSetSpec]:
    """Definition + spec tham số (cho client dựng form) — 404 khi mã lạ."""
    resolved = _resolve(session, code=code)
    return resolved.definition, resolved.param_set_spec


def estimate_report_rows(session: Session, *, code: str, raw_params: Mapping[str, object]) -> int:
    """Số dòng dữ liệu của một lượt render với đúng bộ tham số này (lát 5E).

    Cho ngưỡng chuyển-job (bước 19): tham số đi qua CHÍNH bộ kiểm của render —
    một bộ tham số không render được thì cũng không ước lượng được, lỗi 422 nổ
    ở lượt ước lượng thay vì sau khi job đã nằm trong hàng đợi.
    """
    resolved = _resolve(session, code=code)
    bound = validate_params(
        raw_params,
        spec=resolved.param_set_spec,
        ledger_scope=resolved.definition.ledger_scope,
    )
    return count_dataset_rows(
        session,
        dataset=resolved.dataset,
        layout_spec=resolved.layout_spec,
        binds=bound.sql_binds(resolved.dataset.allowed_params),
    )


def render_report(
    session: Session,
    *,
    code: str,
    output_format: Format,
    raw_params: Mapping[str, object],
    today: date,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    row_hook: Callable[[int], None] | None = None,
) -> RenderedReport:
    """Một lượt kết xuất trọn vẹn: kiểm tham số → chạy SQL → nhóm → PDF/XLSX.

    `row_hook` (lát 5E) được gọi với số thứ tự của TỪNG dòng dữ liệu ngay khi
    dòng đó rời khỏi cursor — job render nền dùng nó để báo tiến độ và kiểm cờ
    hủy ở ranh giới lô. Hook ném ngoại lệ (vd `JobCancelled`) thì cả pipeline
    streaming dừng theo — render là phép đọc, không có trạng thái dở dang để lo.
    """
    resolved = _resolve(session, code=code)
    bound = validate_params(
        raw_params,
        spec=resolved.param_set_spec,
        ledger_scope=resolved.definition.ledger_scope,
    )
    param_lines = _param_lines_with_branches(session, bound)
    rows: Iterator[Mapping[str, object]] = execute_dataset(
        session,
        dataset=resolved.dataset,
        layout_spec=resolved.layout_spec,
        binds=bound.sql_binds(resolved.dataset.allowed_params),
    )
    if row_hook is not None:
        rows = _with_row_hook(rows, row_hook)
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
            options=options,
        )
        media_type = PDF_MEDIA_TYPE
    elif output_format == "xlsx":
        content = render_xlsx(
            title=title,
            unit=unit,
            param_lines=param_lines,
            layout_spec=resolved.layout_spec,
            display_rows=display_rows,
            options=options,
        )
        media_type = XLSX_MEDIA_TYPE
    else:
        assert_never(output_format)

    filename = (
        f"{resolved.definition.code}-{bound.from_date:%Y%m%d}-{bound.to_date:%Y%m%d}"
        f".{output_format}"
    )
    return RenderedReport(content=content, media_type=media_type, filename=filename)


def preview_report(
    session: Session,
    *,
    code: str,
    raw_params: Mapping[str, object],
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
) -> ReportPreview:
    """Xem trước dạng lưới (bước 14 phase-05): cùng đường chạy với render —
    metadata → tham số → SQL → nhóm → pha chữ — chỉ dừng trước pha giấy.

    Bản xem trước và bản in vì thế là MỘT con số: không có nhánh "preview tính
    kiểu khác" để hai bên lệch nhau (BR-RPT-02 ở tầng trình bày).
    """
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
    collected: list[PresentationRow] = []
    truncated = False
    for row in presentation_rows(
        display_rows,
        layout_spec=resolved.layout_spec,
        quantity_decimals=options.quantity_decimals,
    ):
        if len(collected) >= PREVIEW_MAX_ROWS:
            truncated = True
            break
        collected.append(row)
    return ReportPreview(
        code=resolved.definition.code,
        name=resolved.definition.name,
        param_lines=param_lines,
        layout_spec=resolved.layout_spec,
        rows=tuple(collected),
        truncated=truncated,
    )


def _with_row_hook(
    rows: Iterator[Mapping[str, object]], hook: Callable[[int], None]
) -> Iterator[Mapping[str, object]]:
    for index, row in enumerate(rows, start=1):
        hook(index)
        yield row


def _resolve(session: Session, *, code: str) -> _ResolvedReport:
    definition = session.execute(
        # Cùng điều kiện scheme với danh mục (H2): một mã mẫu thông tư khác
        # không render/preview được — 404 y như mã không tồn tại, vì với dữ
        # liệu kế toán này thì nó ĐÚNG là không tồn tại.
        select(ReportDefinition).where(ReportDefinition.code == code, _scheme_condition(session))
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
