"""Statement builder: layout + kỳ + sổ → hai cột số của một BCTC (FR-GLE-043).

Ngữ nghĩa cột theo `statement_kind` (khai ở `kernel/config/statements/models.py`):

* `balance_sheet` — cột chính = số dư tại **cuối kỳ** đã chọn; cột so sánh =
  "Số đầu năm" (dư `opening_balances` của chính năm đó).
* `income`/`cashflow` — cột chính = phát sinh **lũy kế từ đầu năm** tới cuối kỳ
  ("Năm nay"); cột so sánh ("Năm trước") = **None, chưa lập được ở v1** — xem
  bất biến dưới.

**Bất biến cột phát sinh (review 5B, H1):** công thức B02 đọc phát sinh thô
(`DR_PS`/`CR_PS`), mà bút toán kết chuyển đổ vào CHÍNH các phát sinh đó (Nợ 511
/ Có 911…). Trên một năm **đã kết chuyển**, 9/10 chỉ tiêu B02 thành số rác —
Công báo định nghĩa các chỉ tiêu này theo *phát sinh đối ứng 911*, thứ đòi cờ
nhận diện bút toán kết chuyển trên chứng từ (chưa có, quyết định sản phẩm của
10a). Hệ quả: cột chính chỉ đúng khi kỳ xem CHƯA chạy kết chuyển (ghi trong
`note` của chỉ tiêu 01/02); còn cột "Năm trước" — năm trước muốn B01 cân thì
BẮT BUỘC đã kết chuyển — không có trạng thái nào cho số đúng, nên builder trả
`None` thay vì một con số sai lặng lẽ. 10a mang cờ loại-trừ-kết-chuyển thì bật
lại cột này.

Gói cấu hình chọn theo `resolve_package(scheme của năm, ngày cuối kỳ)` — đổi
chế độ kế toán là đổi trọn bộ layout, không sửa code (LD-06, FR-NFR-070).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.config.statements.formula.evaluator import evaluate_rows
from ket.kernel.config.statements.formula.parser import FormulaNode, parse_formula
from ket.kernel.config.statements.models import StatementKind, StatementLayout, StatementRow
from ket.kernel.errors import PeriodNotFoundError, StatementLayoutNotFoundError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.reporting.statements.balance_source import load_fiscal_year_amounts


@dataclass(frozen=True)
class StatementRowValue:
    """Một chỉ tiêu đã tính: giá trị cột chính + cột so sánh, kèm dữ liệu trình bày."""

    row_code: str
    label: str
    label_en: str | None
    note_ref: str | None
    indent_level: int
    is_bold: bool
    hide_when_zero: bool
    value: Decimal
    comparative: Decimal | None
    """`None` = cột so sánh chưa lập được (layout phát sinh, chờ 10a — xem
    docstring đầu tệp). Client hiện "—", không hiện 0: số 0 là một khẳng định
    kế toán, còn "chưa lập được" là sự thật."""


@dataclass(frozen=True)
class StatementResult:
    """Một BCTC đã lập, theo đúng thứ tự trình bày của mẫu."""

    layout_code: str
    name: str
    name_en: str | None
    statement_kind: str
    package_code: str
    ledger: int
    period_id: int
    branch_id: int | None
    rows: tuple[StatementRowValue, ...]


@dataclass(frozen=True)
class _LayoutRows:
    layout: StatementLayout
    rows: tuple[StatementRow, ...]
    formulas: dict[str, FormulaNode]


def list_layouts(session: Session, *, period_id: int) -> tuple[str, tuple[StatementLayout, ...]]:
    """`(mã gói hiệu lực, mọi layout của gói)` cho kỳ này, layout theo mã mẫu.

    Mã gói trả về cả khi gói không có layout nào (gói `.zip` nhập ngoài chỉ
    mang hệ TK) — client vẫn cần biết gói nào đang hiệu lực (review 5B, L-4).
    """
    _, _, package_id, package_code = _resolve_context(session, period_id)
    layouts = tuple(
        session.execute(
            select(StatementLayout)
            .where(StatementLayout.package_id == package_id)
            .order_by(StatementLayout.code)
        )
        .scalars()
        .all()
    )
    return package_code, layouts


def build_statement(
    session: Session,
    *,
    layout_code: str,
    period_id: int,
    ledger: int,
    branch_id: int | None = None,
) -> StatementResult:
    """Lập một BCTC cho `(layout, kỳ, sổ[, chi nhánh])`.

    `branch_id` chỉ thu hẹp thêm trong phạm vi RLS của người gọi (cùng hợp đồng
    với bảng cân đối TK) — BCTC hợp pháp của toàn doanh nghiệp là bản không
    truyền `branch_id`, cộng mọi chi nhánh người gọi thấy (BR-RPT-05: cộng theo
    dòng phát sinh, không cộng qua nút cha–con nên không cộng trùng).
    """
    period, year, package_id, package_code = _resolve_context(session, period_id)
    layout_rows = _load_layout(session, package_id=package_id, layout_code=layout_code)

    closing_column, opening_column = load_fiscal_year_amounts(
        session,
        ledger=ledger,
        fiscal_year_id=year.id,
        year_start=year.start_date,
        range_end=period.end_date,
        branch_id=branch_id,
    )

    values = evaluate_rows(layout_rows.formulas, closing_column)
    if layout_rows.layout.statement_kind == StatementKind.BALANCE_SHEET:
        comparative_values: dict[str, Decimal] | None = evaluate_rows(
            layout_rows.formulas, opening_column
        )
    else:
        # Cột "Năm trước" của layout phát sinh chưa lập được — xem bất biến
        # H1 ở docstring đầu tệp. None, không phải 0.
        comparative_values = None

    return StatementResult(
        layout_code=layout_rows.layout.code,
        name=layout_rows.layout.name,
        name_en=layout_rows.layout.name_en,
        statement_kind=layout_rows.layout.statement_kind,
        package_code=package_code,
        ledger=ledger,
        period_id=period_id,
        branch_id=branch_id,
        rows=tuple(
            StatementRowValue(
                row_code=row.row_code,
                label=row.label,
                label_en=row.label_en,
                note_ref=row.note_ref,
                indent_level=row.indent_level,
                is_bold=row.is_bold,
                hide_when_zero=row.hide_when_zero,
                value=values[row.row_code],
                comparative=(
                    comparative_values[row.row_code] if comparative_values is not None else None
                ),
            )
            for row in layout_rows.rows
        ),
    )


def _resolve_context(
    session: Session, period_id: int
) -> tuple[AccountingPeriod, FiscalYear, int, str]:
    period = session.get(AccountingPeriod, period_id)
    if period is None:
        raise PeriodNotFoundError("Kỳ kế toán không tồn tại", period_id=period_id)
    year = session.get(FiscalYear, period.fiscal_year_id)
    if year is None:  # pragma: no cover - FK bảo đảm
        raise RuntimeError(f"Kỳ {period_id} trỏ vào năm tài chính không tồn tại")
    package = resolve_package(session, scheme=year.accounting_scheme, on_date=period.end_date)
    return period, year, package.id, package.code


def _load_layout(session: Session, *, package_id: int, layout_code: str) -> _LayoutRows:
    layout = session.execute(
        select(StatementLayout).where(
            StatementLayout.package_id == package_id, StatementLayout.code == layout_code
        )
    ).scalar_one_or_none()
    if layout is None:
        raise StatementLayoutNotFoundError(
            "Gói cấu hình đang hiệu lực không có mẫu BCTC mang mã này",
            layout_code=layout_code,
            package_id=package_id,
        )
    rows = tuple(
        session.execute(
            select(StatementRow)
            .where(StatementRow.layout_id == layout.id)
            .order_by(StatementRow.display_order)
        )
        .scalars()
        .all()
    )
    # Công thức đã qua loader fail-closed lúc vào DB; parse lại ở đây vì đây là
    # ranh giới cuối trước khi con số lên BCTC — một dòng bị sửa tay trong DB
    # phải nổ thành lỗi cấu hình rõ ràng, không thành con số sai lặng lẽ.
    formulas = {row.row_code: parse_formula(row.formula) for row in rows}
    return _LayoutRows(layout=layout, rows=rows, formulas=formulas)
