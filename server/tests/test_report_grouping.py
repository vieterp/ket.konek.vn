"""Tổng nhóm streaming (`reporting/engine/grouping.py`) — Decimal từ đầu tới cuối."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.kernel.config.reports.spec import parse_layout_spec
from ket.kernel.errors import ReportDatasetInvalidError
from ket.reporting.engine.grouping import (
    DataRow,
    GrandTotal,
    GroupFooter,
    GroupHeader,
    group_rows,
)

LAYOUT = parse_layout_spec(
    {
        "columns": [
            {"key": "description", "label": "Diễn giải", "type": "text"},
            {"key": "debit", "label": "Nợ", "type": "money"},
        ],
        "group_by": [{"key": "account_code", "heading_keys": ["account_code", "account_name"]}],
        "totals": ["debit"],
        "sort": ["account_code"],
    },
    layout_code="t",
)

TWO_LEVEL = parse_layout_spec(
    {
        "columns": [
            {"key": "description", "label": "Diễn giải", "type": "text"},
            {"key": "debit", "label": "Nợ", "type": "money"},
        ],
        "group_by": [
            {"key": "branch_code", "heading_keys": ["branch_code"]},
            {"key": "account_code", "heading_keys": ["account_code"]},
        ],
        "totals": ["debit"],
        "sort": ["branch_code", "account_code"],
    },
    layout_code="t2",
)


def _row(account: str, name: str, amount: int, branch: str = "CN1") -> dict[str, object]:
    return {
        "account_code": account,
        "account_name": name,
        "branch_code": branch,
        "description": f"dòng {account}",
        "debit": Decimal(amount),
    }


class TestSingleLevel:
    def test_groups_totals_and_grand_total(self) -> None:
        rows = [_row("111", "Tiền mặt", 100), _row("111", "Tiền mặt", 50), _row("112", "NH", 7)]
        out = list(group_rows(iter(rows), layout_spec=LAYOUT))
        kinds = [type(item).__name__ for item in out]
        assert kinds == [
            "GroupHeader",
            "DataRow",
            "DataRow",
            "GroupFooter",
            "GroupHeader",
            "DataRow",
            "GroupFooter",
            "GrandTotal",
        ]
        first_footer = out[3]
        assert isinstance(first_footer, GroupFooter)
        assert first_footer.heading == "111 — Tiền mặt"
        assert first_footer.totals["debit"] == Decimal(150)
        grand = out[-1]
        assert isinstance(grand, GrandTotal)
        assert grand.totals["debit"] == Decimal(157)
        assert grand.row_count == 3

    def test_data_cells_follow_column_order(self) -> None:
        out = list(group_rows(iter([_row("111", "TM", 9)]), layout_spec=LAYOUT))
        data = next(item for item in out if isinstance(item, DataRow))
        assert data.cells == ("dòng 111", Decimal(9))

    def test_empty_input_yields_zero_grand_total(self) -> None:
        """ "Không có phát sinh" là một khẳng định — trang in vẫn có dòng tổng 0."""
        out = list(group_rows(iter([]), layout_spec=LAYOUT))
        assert len(out) == 1
        grand = out[0]
        assert isinstance(grand, GrandTotal)
        assert grand.totals["debit"] == Decimal(0)
        assert grand.row_count == 0

    def test_non_numeric_total_column_is_a_config_error(self) -> None:
        """Cột `money` mà SQL trả text = lỗi cấu hình dataset↔layout (422),
        không phải TypeError → 500 (review 5C, L3): probe `LIMIT 0` không đo
        được kiểu ô vì không có dòng — đây là hàng rào lúc chạy."""
        bad = _row("111", "TM", 0)
        bad["debit"] = "1000"
        with pytest.raises(ReportDatasetInvalidError):
            list(group_rows(iter([bad]), layout_spec=LAYOUT))


class TestTwoLevel:
    def test_inner_groups_close_before_outer(self) -> None:
        rows = [
            _row("111", "TM", 10, branch="CN1"),
            _row("112", "NH", 20, branch="CN1"),
            _row("111", "TM", 5, branch="CN2"),
        ]
        out = list(group_rows(iter(rows), layout_spec=TWO_LEVEL))
        headers = [item.heading for item in out if isinstance(item, GroupHeader)]
        assert headers == ["CN1", "111", "112", "CN2", "111"]
        footers = [
            (item.level, item.heading, item.totals["debit"])
            for item in out
            if isinstance(item, GroupFooter)
        ]
        # Nhóm trong đóng trước nhóm ngoài; tổng cấp ngoài gồm mọi cấp trong.
        assert footers == [
            (1, "111", Decimal(10)),
            (1, "112", Decimal(20)),
            (0, "CN1", Decimal(30)),
            (1, "111", Decimal(5)),
            (0, "CN2", Decimal(5)),
        ]
