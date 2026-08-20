"""Tổng nhóm streaming: dòng dữ liệu (đã sắp) → dòng trình bày (FR-RPT-001).

Đọc MỘT lượt, nhớ MỘT nhóm đang mở — không gom cả kết quả vào RAM. Điều kiện
tiên quyết (khóa nhóm là tiền tố của `sort`) đã kiểm ở `parse_layout_spec`,
nên "dòng cùng nhóm liền nhau" là bất biến, không phải hy vọng.

Tiền vẫn là `Decimal` từ đầu tới cuối (LD-03: mọi phép cộng ở server); renderer
mới là nơi đổi thành chữ/định dạng ô.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

from ket.kernel.config.reports.spec import LayoutSpec
from ket.kernel.errors import ReportDatasetInvalidError

ZERO = Decimal(0)


@dataclass(frozen=True)
class GroupHeader:
    """Mở một nhóm: `heading` ghép từ `heading_keys` ("111 — Tiền mặt")."""

    level: int
    heading: str


@dataclass(frozen=True)
class DataRow:
    """Một dòng dữ liệu; `cells` theo đúng thứ tự cột của layout."""

    cells: tuple[object, ...]


@dataclass(frozen=True)
class GroupFooter:
    """Đóng một nhóm: tổng các cột `totals` của riêng nhóm đó."""

    level: int
    heading: str
    totals: Mapping[str, Decimal]
    row_count: int


@dataclass(frozen=True)
class GrandTotal:
    totals: Mapping[str, Decimal]
    row_count: int


type DisplayRow = GroupHeader | DataRow | GroupFooter | GrandTotal


@dataclass
class _OpenGroup:
    level: int
    key_value: object
    heading: str
    totals: dict[str, Decimal]
    row_count: int


def _as_decimal(value: object) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    # Lỗi cấu hình dataset↔layout (cột `money` mà SQL trả text) — trả lỗi
    # nghiệp vụ 422 thay vì TypeError → 500 (review 5C, L3): probe `LIMIT 0`
    # không đo được KIỂU Ô vì không có dòng, nên đây là hàng rào lúc chạy.
    raise ReportDatasetInvalidError(f"Cột tổng phải là số, dataset trả {type(value).__name__}")


def _heading(row: Mapping[str, object], heading_keys: tuple[str, ...]) -> str:
    parts = [str(row[key]) for key in heading_keys if row[key] is not None]
    return " — ".join(parts)


def group_rows(
    rows: Iterator[Mapping[str, object]], *, layout_spec: LayoutSpec
) -> Iterator[DisplayRow]:
    """Dòng dữ liệu đã sắp theo khóa nhóm → dòng trình bày.

    Báo cáo không nhóm (`group_by` rỗng) vẫn kết thúc bằng `GrandTotal` khi
    layout khai `totals`; kết quả rỗng cho ra đúng một `GrandTotal` toàn 0 —
    "không có phát sinh" là một khẳng định, trang in vẫn phải có dòng tổng.
    """
    column_keys = tuple(column.key for column in layout_spec.columns)
    total_keys = layout_spec.totals
    open_groups: list[_OpenGroup] = []
    grand = dict.fromkeys(total_keys, ZERO)
    grand_count = 0

    for row in rows:
        # Đóng các nhóm đã đổi khóa — từ cấp SÂU nhất trở lên cấp đổi đầu tiên.
        changed_from = None
        for index, group in enumerate(layout_spec.group_by):
            if index >= len(open_groups) or open_groups[index].key_value != row[group.key]:
                changed_from = index
                break
        if changed_from is not None:
            yield from _close_groups(open_groups, down_to=changed_from)
            for index in range(changed_from, len(layout_spec.group_by)):
                group = layout_spec.group_by[index]
                opened = _OpenGroup(
                    level=index,
                    key_value=row[group.key],
                    heading=_heading(row, group.heading_keys),
                    totals=dict.fromkeys(total_keys, ZERO),
                    row_count=0,
                )
                open_groups.append(opened)
                yield GroupHeader(level=index, heading=opened.heading)

        for open_group in open_groups:
            for key in total_keys:
                open_group.totals[key] += _as_decimal(row[key])
            open_group.row_count += 1
        for key in total_keys:
            grand[key] += _as_decimal(row[key])
        grand_count += 1
        yield DataRow(cells=tuple(row[key] for key in column_keys))

    yield from _close_groups(open_groups, down_to=0)
    if total_keys:
        yield GrandTotal(totals=dict(grand), row_count=grand_count)


def _close_groups(open_groups: list[_OpenGroup], *, down_to: int) -> Iterator[GroupFooter]:
    while len(open_groups) > down_to:
        group = open_groups.pop()
        yield GroupFooter(
            level=group.level,
            heading=group.heading,
            totals=dict(group.totals),
            row_count=group.row_count,
        )
