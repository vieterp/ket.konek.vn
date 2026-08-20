"""Pha CHỮ dùng chung của PDF và preview JSON: dòng trình bày → ô đã định dạng.

Tách khỏi `pdf_renderer` ở lát 5D khi preview lưới (bước 14) cần đúng cùng một
phép trình bày: cùng quy ước số Việt Nam, cùng ô-0-để-trống, cùng nhãn dòng
tổng — bản xem trước trên màn hình và bản in phải là MỘT con số, chỉ khác tờ
giấy (BR-RPT-02 ở tầng trình bày). XLSX không đi qua đây (giữ giá trị số +
`num_format`, FR-RPT-012).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal
from typing import Literal, TypedDict

from ket.kernel.config.reports.spec import ColumnSpec, ColumnType, LayoutSpec
from ket.kernel.errors import ReportDatasetInvalidError
from ket.reporting.engine.grouping import (
    DataRow,
    DisplayRow,
    GrandTotal,
    GroupFooter,
    GroupHeader,
)
from ket.reporting.rendering import formats

RowKind = Literal["data", "group_header", "group_footer", "grand_total"]


class Cell(TypedDict):
    text: str
    css: str


def column_css(column: ColumnSpec) -> str:
    css = f"cell-{column.type}"
    if column.align is not None:
        css += f" cell-{column.align}"
    return css


def format_cell(column: ColumnSpec, value: object, *, quantity_decimals: int = 2) -> str:
    if column.type in (ColumnType.MONEY, ColumnType.QUANTITY):
        amount = _as_amount(column, value)
        if column.type == ColumnType.MONEY:
            return formats.format_money(amount, blank_zero=True)
        return formats.format_quantity(amount, decimals=quantity_decimals)
    if column.type == ColumnType.DATE:
        if value is not None and not isinstance(value, date):
            raise ReportDatasetInvalidError(
                f"Cột {column.key!r} khai kiểu date, dataset trả {type(value).__name__}",
                column_key=column.key,
            )
        return formats.format_date(value)
    return formats.format_text(value)


def _as_amount(column: ColumnSpec, value: object) -> Decimal | None:
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise ReportDatasetInvalidError(
        f"Cột {column.key!r} khai kiểu số, dataset trả {type(value).__name__}",
        column_key=column.key,
    )


def total_cells(
    columns: tuple[ColumnSpec, ...],
    first_total_index: int,
    label: str,
    totals: Mapping[str, Decimal],
) -> list[Cell]:
    cells: list[Cell] = [{"text": label, "css": "cell-left"}]
    for column in columns[first_total_index:]:
        if column.key in totals:
            cells.append(
                {
                    "text": formats.format_money(totals[column.key], blank_zero=False),
                    "css": column_css(column),
                }
            )
        else:
            cells.append({"text": "", "css": column_css(column)})
    return cells


class PresentationRow(TypedDict, total=False):
    kind: RowKind
    heading: str
    cells: list[Cell]
    label_span: int


def first_total_index_of(layout_spec: LayoutSpec) -> int:
    total_keys = frozenset(layout_spec.totals)
    return next(
        (index for index, column in enumerate(layout_spec.columns) if column.key in total_keys),
        len(layout_spec.columns),
    )


def presentation_rows(
    display_rows: Iterator[DisplayRow],
    *,
    layout_spec: LayoutSpec,
    quantity_decimals: int = 2,
) -> Iterator[PresentationRow]:
    columns = layout_spec.columns
    first_total_index = first_total_index_of(layout_spec)
    for row in display_rows:
        if isinstance(row, GroupHeader):
            yield {"kind": "group_header", "heading": row.heading}
        elif isinstance(row, DataRow):
            yield {
                "kind": "data",
                "cells": [
                    {
                        "text": format_cell(column, value, quantity_decimals=quantity_decimals),
                        "css": column_css(column),
                    }
                    for column, value in zip(columns, row.cells, strict=True)
                ],
            }
        elif isinstance(row, GroupFooter):
            yield {
                "kind": "group_footer",
                "label_span": first_total_index,
                "cells": total_cells(columns, first_total_index, f"Cộng {row.heading}", row.totals),
            }
        elif isinstance(row, GrandTotal):
            yield {
                "kind": "grand_total",
                "label_span": first_total_index,
                "cells": total_cells(columns, first_total_index, "Tổng cộng", row.totals),
            }
