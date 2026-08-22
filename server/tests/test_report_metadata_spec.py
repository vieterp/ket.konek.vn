"""Spec + scope + loader của report engine (lát 5C) — thuần dữ liệu, không DB.

Ba ranh giới fail-closed được chứng minh ở đây:

* `parse_layout_spec`/`parse_param_set_spec` — JSONB thô sai hình dạng nổ thành
  `ReportSpecInvalidError`, không thành `KeyError` giữa lượt render.
* `compose_scoped_query`/`assert_placeholders_allowed` — không có đường nối
  chuỗi tự do: placeholder ngoài `allowed_params` và identifier sắp xếp lạ đều
  bị chặn.
* `load_builtin_reports` — dữ liệu builtin đóng gói phải tự đủ và đúng hợp
  đồng ngay lúc test, không đợi tới lúc cấp dữ liệu kế toán.
"""

from __future__ import annotations

import pytest

from ket.kernel.config.reports.loader import load_builtin_reports
from ket.kernel.config.reports.models import ReportDataset
from ket.kernel.config.reports.scope import (
    assert_placeholders_allowed,
    compose_scoped_query,
    sql_placeholders,
)
from ket.kernel.config.reports.spec import (
    STANDARD_PARAMS,
    parse_layout_spec,
    parse_param_set_spec,
)
from ket.kernel.errors import ReportDatasetInvalidError, ReportSpecInvalidError

BASE_LAYOUT: dict[str, object] = {
    "columns": [
        {"key": "posting_date", "label": "Ngày", "type": "date"},
        {"key": "description", "label": "Diễn giải", "type": "text"},
        {"key": "debit", "label": "Nợ", "type": "money"},
        {"key": "credit", "label": "Có", "type": "money"},
    ],
    "group_by": [{"key": "account_code", "heading_keys": ["account_code", "account_name"]}],
    "totals": ["debit", "credit"],
    "sort": ["account_code", "posting_date"],
}


class TestLayoutSpec:
    def test_valid_layout_parses(self) -> None:
        spec = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        assert [c.key for c in spec.columns] == ["posting_date", "description", "debit", "credit"]
        assert spec.page.orientation == "portrait"

    def test_duplicate_column_keys_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["columns"] = [
            {"key": "debit", "label": "Nợ", "type": "money"},
            {"key": "debit", "label": "Nợ 2", "type": "money"},
        ]
        raw["totals"] = []
        raw["group_by"] = []
        raw["sort"] = ["debit"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_total_must_be_displayed_money_column(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["totals"] = ["description"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")
        raw["totals"] = ["so_tien_khong_hien_thi"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_group_keys_must_prefix_sort(self) -> None:
        """Grouping streaming chỉ đúng khi dòng cùng nhóm liền nhau — layout vi
        phạm phải chết lúc parse, không cho ra tổng nhóm sai lặng lẽ."""
        raw = dict(BASE_LAYOUT)
        raw["sort"] = ["posting_date", "account_code"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_unknown_field_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["colums"] = raw.pop("columns")
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_unknown_column_type_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["columns"] = [{"key": "x", "label": "X", "type": "percent"}]
        raw.update(totals=[], group_by=[], sort=["x"])
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")


class TestParamSetSpec:
    def test_standard_param_cannot_be_redeclared(self) -> None:
        raw = {"params": [{"name": "ledger", "kind": "int", "label": "Sổ"}]}
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")

    def test_duplicate_names_rejected(self) -> None:
        raw = {
            "params": [
                {"name": "x", "kind": "int", "label": "X"},
                {"name": "x", "kind": "text", "label": "X2"},
            ]
        }
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")

    def test_unknown_kind_rejected(self) -> None:
        raw = {"params": [{"name": "x", "kind": "json", "label": "X"}]}
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")


class TestScopeComposition:
    def _dataset(self, sql: str, *, allowed: list[str], branch: bool = True) -> ReportDataset:
        return ReportDataset(
            code="t",
            sql_text=sql,
            allowed_params=allowed,
            supports_branch=branch,
            supports_ledger=True,
        )

    def test_placeholders_extraction_ignores_casts_and_strings(self) -> None:
        sql = "SELECT CAST(:a AS INTEGER), x::text, ':not_a_bind', :b_2 FROM t"
        assert sql_placeholders(sql) == frozenset({"a", "b_2"})

    def test_placeholder_outside_allowed_params_rejected(self) -> None:
        dataset = self._dataset("SELECT :rogue FROM t", allowed=["from_date"])
        with pytest.raises(ReportDatasetInvalidError):
            assert_placeholders_allowed(dataset)

    def test_wrapper_adds_scope_and_order(self) -> None:
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        dataset = self._dataset("SELECT 1 AS x", allowed=[])
        composed = compose_scoped_query(dataset, layout)
        assert "scoped.branch_id = ANY(:branch_ids)" in composed
        assert "scoped.ledger = :ledger" in composed
        assert composed.endswith("ORDER BY scoped.account_code, scoped.posting_date")

    def test_wrapper_skips_branch_when_unsupported(self) -> None:
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        dataset = self._dataset("SELECT 1 AS x", allowed=[], branch=False)
        composed = compose_scoped_query(dataset, layout)
        assert "branch_ids" not in composed
        assert "scoped.ledger = :ledger" in composed

    def test_sort_identifier_is_re_validated(self) -> None:
        """Phòng đường gọi không đi qua parser (spec dựng tay trong code)."""
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        hacked = layout.model_copy(update={"sort": ("account_code; DROP TABLE x",)})
        dataset = self._dataset("SELECT 1 AS x", allowed=[])
        with pytest.raises(ReportDatasetInvalidError):
            compose_scoped_query(dataset, hacked)


class TestBuiltinManifest:
    def test_builtin_reports_load_and_are_self_contained(self) -> None:
        loaded = load_builtin_reports()
        codes = {d.code for d in loaded.manifest.definitions}
        # 8 báo cáo bộ sổ theo đúng mã mẫu thông tư (5D — đóng câu hỏi mở #1
        # của 5C: mã trung lập `SO-CAI` thay bằng `S03b-DN`).
        assert {
            "S03a-DN",
            "S03b-DN",
            "S38-DN",
            "S06-DN",
            "S03a-DNN",
            "S03b-DNN",
            "S19-DNN",
            "F01-DNN",
        } <= codes
        assert "SO-CAI" not in codes
        for entry in loaded.manifest.datasets:
            placeholders = sql_placeholders(loaded.sql_by_dataset[entry.code])
            assert placeholders <= frozenset(entry.allowed_params)

    def test_builtin_dataset_count_stays_under_project_cap(self) -> None:
        """Trần **≤30 dataset phủ ~155 báo cáo** (plan.md / phase-05 §Chiến lược
        quy mô: "Vượt 40 thì dừng và xem lại thiết kế").

        Con số này thay cho trần ≤5 của thời phase 5 — trần đó là cách diễn đạt
        CÙNG một mục tiêu khi mới có 10 báo cáo đầu ("3 dataset × nhiều layout",
        không phải một dataset một báo cáo), và nó hết dùng được ngay khi phân
        hệ đầu tiên đăng ký bộ báo cáo của mình. Điều cần canh là mục tiêu đo
        được của dự án, không phải cái mốc tạm.
        """
        loaded = load_builtin_reports()
        assert len(loaded.manifest.datasets) <= 30

    def test_look_alike_forms_share_one_dataset(self) -> None:
        """Mẫu sổ chỉ khác nhau một tham số phải dùng CHUNG dataset.

        Đây mới là bất biến mà trần đếm ở trên tồn tại để bảo vệ, và nó kiểm
        được thẳng thay vì gián tiếp qua một con số: `S07a-DN`/`S08-DN` là cùng
        sổ chi tiết một tài khoản tiền (khác nhóm TK), `S03a1-DN`/`S03a2-DN` là
        cùng nhật ký chuyên dùng (khác chiều tiền). Tách chúng thành hai dataset
        là nhân đôi một câu SQL — chính thứ `fixed_params` sinh ra để tránh.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        for left, right, param in (
            ("S07a-DN", "S08-DN", "account_prefix"),
            ("S03a1-DN", "S03a2-DN", "direction"),
        ):
            assert by_code[left].dataset_code == by_code[right].dataset_code
            assert by_code[left].param_set_code == by_code[right].param_set_code
            # Cùng dataset thì phải khác nhau ở ĐÚNG tham số ghim, nếu không hai
            # mã mẫu khác nhau sẽ in ra cùng một tờ giấy.
            assert by_code[left].fixed_params[param] != by_code[right].fixed_params[param]
            assert by_code[left].layout_code != by_code[right].layout_code

    def test_scheme_bound_definitions_declare_known_scheme(self) -> None:
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        assert by_code["S03b-DN"].package_scheme == "TT99"
        assert by_code["F01-DNN"].package_scheme == "TT133"

    def test_builtin_extra_params_are_not_standard(self) -> None:
        loaded = load_builtin_reports()
        for spec in loaded.param_set_specs.values():
            assert not STANDARD_PARAMS.intersection(p.name for p in spec.params)
