"""Tham số GHIM của một báo cáo (`report_definitions.fixed_params`, lát 6E-1).

Ghim tham số là thứ cho hai mẫu sổ khác nhau ĐÚNG một tham số dùng chung một
dataset — `S03a1-DN` (Nhật ký thu tiền) và `S03a2-DN` (chi tiền) là cùng câu
SQL với `direction` khác nhau. Ba nhóm bất biến:

* dữ liệu builtin sai phải nổ lúc NẠP, không lúc người dùng bấm Xem;
* giá trị ghim thắng, và thắng **tường minh** — client gửi giá trị khác là lỗi,
  không phải thứ bị lặng lẽ ghi đè (cùng doctrine `ledger_scope`, BR-RPT-04);
* giá trị ghim phải đi tới tận bind SQL, không rơi lại giá trị mặc định.
"""

from __future__ import annotations

import pytest

from ket.kernel.config.reports.loader import DefinitionEntry, _assert_fixed_params_wired
from ket.kernel.config.reports.spec import ParamSetSpec, ParamSpec
from ket.kernel.errors import ReportParamsInvalidError, ReportSpecInvalidError
from ket.reporting.engine.params import validate_params

_DIRECTION = ParamSpec(name="direction", kind="text", label="Chiều tiền", required=True)
_THRESHOLD = ParamSpec(name="threshold", kind="int", label="Ngưỡng", required=False)
_SPEC = ParamSetSpec(params=(_DIRECTION, _THRESHOLD))


def _definition(fixed: dict[str, object]) -> DefinitionEntry:
    return DefinitionEntry(
        code="TEST-PIN",
        name="Báo cáo thử",
        category="quy-tien-mat",
        module="cash_book",
        dataset_code="cash_journal",
        layout_code="cash-journal-in",
        param_set_code="cash_journal_params",
        fixed_params=fixed,
    )


class TestManifestValidation:
    """Dữ liệu builtin sai là lỗi của BẢN CÀI — phải nổ lúc nạp."""

    def test_a_pinned_name_must_be_declared_in_the_param_set(self) -> None:
        with pytest.raises(ReportSpecInvalidError, match="không khai trong bộ tham số"):
            _assert_fixed_params_wired(_definition({"khong_co": "x"}), _SPEC)

    def test_standard_params_cannot_be_pinned(self) -> None:
        # `ledger` đã có `ledger_scope` làm đúng việc này; hai cỗ máy cho cùng
        # một quyết định là đường để chúng bất đồng.
        with pytest.raises(ReportSpecInvalidError, match="bộ chuẩn"):
            _assert_fixed_params_wired(_definition({"ledger": 1}), _SPEC)

    def test_a_pinned_value_must_match_the_declared_kind(self) -> None:
        with pytest.raises(ReportSpecInvalidError, match="không đúng kiểu"):
            _assert_fixed_params_wired(_definition({"threshold": "không-phải-số"}), _SPEC)

    def test_a_well_formed_pin_passes(self) -> None:
        _assert_fixed_params_wired(_definition({"direction": "thu", "threshold": 5}), _SPEC)


class TestRenderTime:
    def test_the_pinned_value_reaches_the_sql_binds(self) -> None:
        bound = validate_params(
            {"from_date": "2026-01-01", "to_date": "2026-01-31"},
            spec=_SPEC,
            ledger_scope="both",
            fixed_params={"direction": "chi"},
        )
        assert dict(bound.extras)["direction"] == "chi"
        assert bound.sql_binds(["from_date", "to_date", "direction"])["direction"] == "chi"

    def test_a_pinned_param_is_echoed_on_the_printout(self) -> None:
        # BR-RPT-03: người đọc tờ in phải biết nó là sổ THU hay sổ CHI, và
        # tham số ghim đi qua đúng cỗ máy thuật-lại của tham số thường.
        bound = validate_params(
            {"from_date": "2026-01-01", "to_date": "2026-01-31"},
            spec=_SPEC,
            ledger_scope="both",
            fixed_params={"direction": "thu"},
        )
        assert any("Chiều tiền: thu" in line for line in bound.echo_lines)

    def test_a_conflicting_client_value_is_refused_not_overridden(self) -> None:
        with pytest.raises(ReportParamsInvalidError, match="ghim cố định"):
            validate_params(
                {"from_date": "2026-01-01", "to_date": "2026-01-31", "direction": "chi"},
                spec=_SPEC,
                ledger_scope="both",
                fixed_params={"direction": "thu"},
            )

    def test_resending_the_same_value_is_accepted(self) -> None:
        # Client tải catalog rồi gửi lại nguyên bộ tham số là chuyện thường —
        # nó không mâu thuẫn với gì cả, nên không có lý do để từ chối.
        bound = validate_params(
            {"from_date": "2026-01-01", "to_date": "2026-01-31", "direction": "thu"},
            spec=_SPEC,
            ledger_scope="both",
            fixed_params={"direction": "thu"},
        )
        assert dict(bound.extras)["direction"] == "thu"

    def test_a_required_param_needs_no_client_value_once_pinned(self) -> None:
        # `direction` là `required=True`: không ghim thì thiếu nó là lỗi. Ghim
        # rồi thì client KHÔNG phải gửi — đó là toàn bộ mục đích của việc ghim.
        with pytest.raises(ReportParamsInvalidError):
            validate_params(
                {"from_date": "2026-01-01", "to_date": "2026-01-31"},
                spec=_SPEC,
                ledger_scope="both",
            )
        validate_params(
            {"from_date": "2026-01-01", "to_date": "2026-01-31"},
            spec=_SPEC,
            ledger_scope="both",
            fixed_params={"direction": "thu"},
        )
