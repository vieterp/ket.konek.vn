"""Kết xuất XLSX: xlsxwriter `constant_memory` — RAM không tăng theo số dòng.

Khác bản PDF ở một điểm nguyên tắc (FR-RPT-012): ô giữ **giá trị số** +
`num_format`, và dòng tổng là **công thức** `SUBTOTAL(9, …)` kèm giá trị đã
tính sẵn — người dùng mở Excel kiểm lại được phép cộng thay vì phải tin một
con số chết. `Decimal` đưa thẳng cho xlsxwriter (nó là `numbers.Number`);
việc XLSX chỉ lưu được double là giới hạn của định dạng tệp, không phải một
phép tính của miền nghiệp vụ (ADR-015 giữ nguyên hiệu lực: mã ở đây không
đụng tới `float`).

`xlsxwriter` không có stub kiểu — import gói gọn trong tệp này (mypy override
ở `pyproject.toml` kèm lý do), cùng cách xử `weasyprint`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal

import xlsxwriter
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

from ket.kernel.config.reports.spec import ColumnSpec, ColumnType, LayoutSpec
from ket.reporting.engine.grouping import (
    DataRow,
    DisplayRow,
    GrandTotal,
    GroupFooter,
    GroupHeader,
)
from ket.reporting.rendering.header import UnitInfo
from ket.reporting.rendering.options import DEFAULT_RENDER_OPTIONS, RenderOptions

_MONEY_FORMAT = "#,##0"
"""VND không phần lẻ theo quy ước sổ sách; định dạng số theo cấu hình người
dùng (FR-RPT-012) vào ở 5D cùng nhóm cấu hình font/logo — giá trị ô vẫn giữ
nguyên phần lẻ, chỉ hiển thị bị làm tròn."""

_DATE_FORMAT = "dd/mm/yyyy"


def _quantity_format(decimals: int) -> str:
    """`num_format` cột số lượng theo cấu hình FR-RPT-012 — giá trị ô giữ
    nguyên phần lẻ, chỉ hiển thị đổi."""
    if decimals <= 0:
        return "#,##0"
    return "#,##0." + "0" * decimals


_COLUMN_CHARS_NUMERATOR = 7
_COLUMN_CHARS_DENOMINATOR = 5
"""Đổi `width` (phần trăm trang in) thành bề ngang cột Excel (~×1,4) — phân số
nguyên vì ADR-015 cấm literal `float` trong `src/ket`, kể cả ngoài miền tiền."""


class _Styles:
    def __init__(self, workbook: xlsxwriter.Workbook, *, quantity_decimals: int) -> None:
        self.title = workbook.add_format({"bold": True, "font_size": 14, "font_color": "#1B365D"})
        self.unit_name = workbook.add_format({"bold": True})
        self.param = workbook.add_format({"italic": True, "font_color": "#475569"})
        self.header = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#F5F7FA",
                "font_color": "#1B365D",
                "border": 1,
                "border_color": "#CBD5E1",
                "text_wrap": True,
                "align": "center",
                "valign": "vcenter",
            }
        )
        base_cell = {"border": 1, "border_color": "#CBD5E1"}
        self.text = workbook.add_format(base_cell)
        self.date = workbook.add_format(
            {**base_cell, "num_format": _DATE_FORMAT, "align": "center"}
        )
        self.money = workbook.add_format({**base_cell, "num_format": _MONEY_FORMAT})
        self.quantity = workbook.add_format(
            {**base_cell, "num_format": _quantity_format(quantity_decimals)}
        )
        self.group_header = workbook.add_format(
            {**base_cell, "bold": True, "bg_color": "#EEF2F7", "font_color": "#1B365D"}
        )
        self.total_label = workbook.add_format({**base_cell, "bold": True, "bg_color": "#F8FAFC"})
        self.total_money = workbook.add_format(
            {**base_cell, "bold": True, "bg_color": "#F8FAFC", "num_format": _MONEY_FORMAT}
        )

    def for_column(self, column: ColumnSpec) -> Format:
        if column.type == ColumnType.MONEY:
            return self.money
        if column.type == ColumnType.QUANTITY:
            return self.quantity
        if column.type == ColumnType.DATE:
            return self.date
        return self.text


def render_xlsx(
    *,
    title: str,
    unit: UnitInfo,
    param_lines: tuple[str, ...],
    layout_spec: LayoutSpec,
    display_rows: Iterator[DisplayRow],
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
) -> bytes:
    """Một báo cáo dạng bảng/nhóm → tệp .xlsx (một sheet, tiêu đề + bảng)."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(
        buffer,
        {"constant_memory": True, "in_memory": False, "default_date_format": _DATE_FORMAT},
    )
    try:
        styles = _Styles(workbook, quantity_decimals=options.quantity_decimals)
        worksheet = workbook.add_worksheet("Báo cáo")
        columns = layout_spec.columns
        _set_column_widths(worksheet, columns)

        row_index = 0
        worksheet.write_string(row_index, 0, unit.name, styles.unit_name)
        row_index += 1
        if unit.address:
            worksheet.write_string(row_index, 0, unit.address)
            row_index += 1
        if unit.tax_code:
            worksheet.write_string(row_index, 0, f"MST: {unit.tax_code}")
            row_index += 1
        row_index += 1
        worksheet.write_string(row_index, 0, title, styles.title)
        row_index += 1
        for line in param_lines:
            worksheet.write_string(row_index, 0, line, styles.param)
            row_index += 1
        row_index += 1

        header_row = row_index
        for column_index, column in enumerate(columns):
            worksheet.write_string(header_row, column_index, column.label, styles.header)
        worksheet.freeze_panes(header_row + 1, 0)
        row_index += 1

        row_index = _write_body(
            worksheet,
            styles,
            layout_spec=layout_spec,
            display_rows=display_rows,
            start_row=row_index,
        )
        del row_index
    finally:
        workbook.close()
    return buffer.getvalue()


def _set_column_widths(worksheet: Worksheet, columns: tuple[ColumnSpec, ...]) -> None:
    for column_index, column in enumerate(columns):
        if column.width is not None:
            worksheet.set_column(
                column_index,
                column_index,
                column.width * _COLUMN_CHARS_NUMERATOR // _COLUMN_CHARS_DENOMINATOR,
            )
        elif column.type == ColumnType.TEXT:
            worksheet.set_column(column_index, column_index, 40)
        else:
            worksheet.set_column(column_index, column_index, 14)


def _column_letter(index: int) -> str:
    letters = ""
    remaining = index
    while True:
        remaining, digit = divmod(remaining, 26)
        letters = chr(ord("A") + digit) + letters
        if remaining == 0:
            return letters
        remaining -= 1


def _write_cell(
    worksheet: Worksheet, row: int, col: int, value: object, cell_format: Format
) -> None:
    if value is None:
        worksheet.write_blank(row, col, None, cell_format)
    elif isinstance(value, Decimal | int):
        worksheet.write_number(row, col, value, cell_format)
    elif isinstance(value, date):
        worksheet.write_datetime(row, col, value, cell_format)
    else:
        worksheet.write_string(row, col, str(value), cell_format)


def _write_totals_row(
    worksheet: Worksheet,
    styles: _Styles,
    *,
    row_index: int,
    label: str,
    totals: Mapping[str, Decimal],
    columns: tuple[ColumnSpec, ...],
    data_start_row: int,
    data_end_row: int,
) -> None:
    """`SUBTOTAL(9, …)` chứ không `SUM`: dòng tổng nhóm lồng trong dải của tổng
    cộng, `SUM` sẽ đếm đôi khi người dùng tự kéo lại công thức — `SUBTOTAL` bỏ
    qua các dòng `SUBTOTAL` khác theo định nghĩa.

    Nhãn nằm ở các cột TRƯỚC cột tổng đầu tiên — cùng phép `first_total_index`
    với bản PDF (review 5C, L4): layout đặt cột tiền ở vị trí 0 không làm mất
    con số tổng của chính cột đó.
    """
    total_keys = frozenset(totals)
    first_total_index = next(
        (index for index, column in enumerate(columns) if column.key in total_keys),
        len(columns),
    )
    if first_total_index > 0:
        worksheet.write_string(row_index, 0, label, styles.total_label)
        for column_index in range(1, first_total_index):
            worksheet.write_blank(row_index, column_index, None, styles.total_label)
    for column_index, column in enumerate(columns):
        if column_index < first_total_index:
            continue
        if column.key in totals:
            letter = _column_letter(column_index)
            if data_end_row >= data_start_row:
                formula = f"=SUBTOTAL(9,{letter}{data_start_row + 1}:{letter}{data_end_row + 1})"
                worksheet.write_formula(
                    row_index, column_index, formula, styles.total_money, totals[column.key]
                )
            else:
                worksheet.write_number(
                    row_index, column_index, totals[column.key], styles.total_money
                )
        else:
            worksheet.write_blank(row_index, column_index, None, styles.total_label)


def _write_body(
    worksheet: Worksheet,
    styles: _Styles,
    *,
    layout_spec: LayoutSpec,
    display_rows: Iterator[DisplayRow],
    start_row: int,
) -> int:
    columns = layout_spec.columns
    row_index = start_row
    body_start = start_row
    group_start_stack: list[int] = []
    for display_row in display_rows:
        if isinstance(display_row, GroupHeader):
            worksheet.write_string(row_index, 0, display_row.heading, styles.group_header)
            for column_index in range(1, len(columns)):
                worksheet.write_blank(row_index, column_index, None, styles.group_header)
            row_index += 1
            group_start_stack.append(row_index)
        elif isinstance(display_row, DataRow):
            for column_index, (column, value) in enumerate(
                zip(columns, display_row.cells, strict=True)
            ):
                _write_cell(worksheet, row_index, column_index, value, styles.for_column(column))
            row_index += 1
        elif isinstance(display_row, GroupFooter):
            data_start = group_start_stack.pop() if group_start_stack else body_start
            _write_totals_row(
                worksheet,
                styles,
                row_index=row_index,
                label=f"Cộng {display_row.heading}",
                totals=display_row.totals,
                columns=columns,
                data_start_row=data_start,
                data_end_row=row_index - 1,
            )
            row_index += 1
        elif isinstance(display_row, GrandTotal):
            _write_totals_row(
                worksheet,
                styles,
                row_index=row_index,
                label="Tổng cộng",
                totals=display_row.totals,
                columns=columns,
                data_start_row=body_start,
                data_end_row=row_index - 1,
            )
            row_index += 1
    return row_index
