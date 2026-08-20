"""Kiểm tham số render (FR-RPT-002, BR-RPT-04) — model Pydantic sinh động."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ket.kernel.config.reports.models import LedgerScope
from ket.kernel.config.reports.spec import parse_param_set_spec
from ket.kernel.errors import ReportParamsInvalidError
from ket.posting.engine.models import Ledger
from ket.reporting.engine.params import validate_params

EMPTY = parse_param_set_spec({"params": []}, param_set_code="p")
WITH_ACCOUNT = parse_param_set_spec(
    {
        "params": [
            {"name": "account_code", "kind": "text", "label": "Tài khoản"},
            {"name": "min_amount", "kind": "decimal", "label": "Số tiền tối thiểu"},
        ]
    },
    param_set_code="p",
)

RANGE = {"from_date": "2026-01-01", "to_date": "2026-12-31"}


class TestStandardParams:
    def test_defaults_applied(self) -> None:
        bound = validate_params(RANGE, spec=EMPTY, ledger_scope=LedgerScope.BOTH)
        assert bound.ledger == int(Ledger.FINANCIAL)
        assert bound.branch_ids is None
        assert bound.from_date == date(2026, 1, 1)

    def test_missing_required_date_rejected(self) -> None:
        with pytest.raises(ReportParamsInvalidError):
            validate_params({"from_date": "2026-01-01"}, spec=EMPTY, ledger_scope=LedgerScope.BOTH)

    def test_reversed_range_rejected(self) -> None:
        with pytest.raises(ReportParamsInvalidError):
            validate_params(
                {"from_date": "2026-12-31", "to_date": "2026-01-01"},
                spec=EMPTY,
                ledger_scope=LedgerScope.BOTH,
            )

    def test_unknown_param_rejected(self) -> None:
        """`extra="forbid"` — tham số gõ nhầm là lỗi, không phải thứ bị lờ đi
        (một bộ lọc bị lờ đi = một báo cáo rộng hơn người dùng tưởng)."""
        with pytest.raises(ReportParamsInvalidError):
            validate_params({**RANGE, "acount": "111"}, spec=EMPTY, ledger_scope=LedgerScope.BOTH)

    def test_empty_branch_list_rejected(self) -> None:
        with pytest.raises(ReportParamsInvalidError):
            validate_params({**RANGE, "branch_ids": []}, spec=EMPTY, ledger_scope=LedgerScope.BOTH)

    def test_ledger_out_of_range_rejected(self) -> None:
        with pytest.raises(ReportParamsInvalidError):
            validate_params({**RANGE, "ledger": 7}, spec=EMPTY, ledger_scope=LedgerScope.BOTH)


class TestLedgerScope:
    def test_fixed_scope_forces_ledger(self) -> None:
        bound = validate_params(RANGE, spec=EMPTY, ledger_scope=LedgerScope.MANAGEMENT)
        assert bound.ledger == int(Ledger.MANAGEMENT)

    def test_conflicting_ledger_is_an_error_not_silently_overridden(self) -> None:
        with pytest.raises(ReportParamsInvalidError):
            validate_params(
                {**RANGE, "ledger": int(Ledger.FINANCIAL)},
                spec=EMPTY,
                ledger_scope=LedgerScope.MANAGEMENT,
            )

    def test_matching_ledger_accepted_on_fixed_scope(self) -> None:
        bound = validate_params(
            {**RANGE, "ledger": int(Ledger.MANAGEMENT)},
            spec=EMPTY,
            ledger_scope=LedgerScope.MANAGEMENT,
        )
        assert bound.ledger == int(Ledger.MANAGEMENT)


class TestExtras:
    def test_extras_coerced_and_bound(self) -> None:
        bound = validate_params(
            {**RANGE, "account_code": "111", "min_amount": "100.50"},
            spec=WITH_ACCOUNT,
            ledger_scope=LedgerScope.BOTH,
        )
        assert dict(bound.extras)["min_amount"] == Decimal("100.50")
        binds = bound.sql_binds(["from_date", "to_date", "account_code"])
        assert binds["account_code"] == "111"
        # `min_amount` không nằm trong allowed_params → không được bind.
        assert "min_amount" not in binds
        # Lớp bọc luôn cần hai bind này, kể cả khi SQL không nhắc tới.
        assert binds["branch_ids"] is None
        assert binds["ledger"] == int(Ledger.FINANCIAL)

    def test_optional_extra_defaults_to_none(self) -> None:
        bound = validate_params(RANGE, spec=WITH_ACCOUNT, ledger_scope=LedgerScope.BOTH)
        assert dict(bound.extras) == {"account_code": None, "min_amount": None}

    def test_echo_lines_describe_parameters(self) -> None:
        """BR-RPT-03: bản in phải thuật lại tham số đã chọn."""
        bound = validate_params(
            {**RANGE, "account_code": "131"}, spec=WITH_ACCOUNT, ledger_scope=LedgerScope.BOTH
        )
        assert "Từ ngày 01/01/2026 đến ngày 31/12/2026" in bound.echo_lines
        assert "Sổ tài chính" in bound.echo_lines
        assert "Tài khoản: 131" in bound.echo_lines
        # Tham số bỏ trống không in ra một dòng "None".
        assert not any("None" in line for line in bound.echo_lines)
