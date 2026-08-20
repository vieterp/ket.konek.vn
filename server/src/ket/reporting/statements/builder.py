"""Statement builder: layout + kỳ + sổ → hai cột số của một BCTC (FR-GLE-043).

Ngữ nghĩa cột theo `statement_kind` (khai ở `kernel/config/statements/models.py`):

* `balance_sheet` — cột chính = số dư tại **cuối kỳ** đã chọn; cột so sánh =
  "Số đầu năm" (dư `opening_balances` của chính năm đó). Đọc **mọi** bút toán,
  kể cả kết chuyển — kết chuyển là thứ làm 421 đúng.
* `income`/`cashflow` — cột chính = phát sinh **lũy kế từ đầu năm** tới cuối kỳ
  ("Năm nay"); cột so sánh = trọn **năm tài chính liền trước** ("Năm trước",
  `None` khi dataset chưa có năm trước). Cả hai cột **bỏ bút toán kết chuyển**
  khỏi phát sinh (LD-17).

**Lịch sử của bất biến này (review 5B, H1 → LD-17):** công thức B02 đọc phát
sinh thô (`DR_PS`/`CR_PS`), mà bút toán kết chuyển đổ vào CHÍNH các phát sinh
đó (Nợ 511 / Có 911…). Lát 5B chưa có cách phân biệt nên B02 sai trên mọi năm
đã kết chuyển, và cột "Năm trước" **không có trạng thái nào cho số đúng** (năm
trước muốn B01 cân thì bắt buộc đã kết chuyển) — 5B trả `None` thay vì số sai
lặng lẽ. Lát này thêm `entry_kind` nên cả hai vấn đề tan: nguồn số lọc kết
chuyển ra khỏi phát sinh, cột "Năm trước" bật lại.

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
from ket.reporting.statements.balance_source import ColumnAmounts, load_fiscal_year_amounts


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
    """`None` = **không có gì để so** — layout phát sinh mà dataset chưa có năm
    tài chính trước. Client hiện "—", không hiện 0: số 0 là một khẳng định kế
    toán ("năm trước không phát sinh"), còn "chưa có năm trước" là sự thật khác
    hẳn."""


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

    is_balance_sheet = layout_rows.layout.statement_kind == StatementKind.BALANCE_SHEET
    closing_column, opening_column = load_fiscal_year_amounts(
        session,
        ledger=ledger,
        fiscal_year_id=year.id,
        year_start=year.start_date,
        range_end=period.end_date,
        branch_id=branch_id,
        operating_turnover_only=not is_balance_sheet,
    )

    values = evaluate_rows(layout_rows.formulas, closing_column)
    if is_balance_sheet:
        comparative_values: dict[str, Decimal] | None = evaluate_rows(
            layout_rows.formulas, opening_column
        )
    else:
        prior = _prior_year_column(
            session,
            current_year=year,
            current_period=period,
            ledger=ledger,
            branch_id=branch_id,
        )
        comparative_values = None if prior is None else evaluate_rows(layout_rows.formulas, prior)

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


def _prior_year_column(
    session: Session,
    *,
    current_year: FiscalYear,
    current_period: AccountingPeriod,
    ledger: int,
    branch_id: int | None,
) -> ColumnAmounts | None:
    """Cột "Năm trước" của layout phát sinh: năm tài chính liền trước, cắt tới
    **kỳ tương ứng**, đã bỏ bút toán kết chuyển (LD-17).

    **Cắt theo kỳ, không lấy trọn năm** (sửa H2 review 4F): báo cáo quý I phải
    so với quý I năm trước, không so với cả năm trước — bản đầu lấy
    `prior.end_date` nên một khoản chi tháng 4 năm ngoái hiện ra trong cột so
    sánh của báo cáo quý I. Chỉ báo cáo cả năm mới so trọn năm, và khi đó "kỳ
    tương ứng" chính là kỳ cuối năm nên cùng một phép tính.

    `None` (khác hẳn "toàn số 0") khi: chưa có năm trước, hoặc năm trước không
    có kỳ mang cùng `period_no` — không bịa ra một mốc cắt khác để lấy bằng
    được một con số.

    Không ràng năm trước phải cùng chế độ kế toán: số đọc theo **số hiệu TK**
    của dòng phát sinh năm đó, nên năm chuyển chế độ vẫn ra số so sánh đúng tới
    mức hai hệ TK còn dùng chung mã.
    """
    prior = session.execute(
        select(FiscalYear)
        .where(FiscalYear.end_date <= current_year.start_date)
        .order_by(FiscalYear.end_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if prior is None:
        return None
    prior_period_end = session.scalar(
        select(AccountingPeriod.end_date).where(
            AccountingPeriod.fiscal_year_id == prior.id,
            AccountingPeriod.period_no == current_period.period_no,
        )
    )
    if prior_period_end is None:
        return None
    turnover_column, _ = load_fiscal_year_amounts(
        session,
        ledger=ledger,
        fiscal_year_id=prior.id,
        year_start=prior.start_date,
        range_end=prior_period_end,
        branch_id=branch_id,
        operating_turnover_only=True,
    )
    return turnover_column


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
