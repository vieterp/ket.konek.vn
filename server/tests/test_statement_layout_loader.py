"""`statements.json` trong gói cấu hình: luật kiểm fail-closed + golden data TT99/TT133.

Nửa đầu: mọi kiểu sai của tệp (công thức hỏng, dải TK không khớp accounts.csv,
rowref lạ, chu trình, mã trùng…) phải bị từ chối NGAY Ở LOADER — trước khi một
dòng nào chạm DB (RT-07, cùng kỷ luật với accounts.csv).

Nửa sau: dữ liệu builtin phải khớp mẫu thông tư về **mã số và thứ tự chỉ tiêu**
(golden test — đối chiếu Phụ lục IV TT99, bản verbatim trong `docs/TT99/`), và
các bất biến cấu trúc: tổng tài sản là [100]+[200], chỉ tiêu (*) không bao giờ
cộng dương, layout phát sinh không dùng hàm số dư tại thời điểm.
"""

from __future__ import annotations

import json

import pytest

from ket.kernel.config.packages.loader import (
    ACCOUNTS_FILE,
    CLOSING_PAIRS_FILE,
    DEFAULT_ACCOUNTS_FILE,
    PACKAGE_MANIFEST_FILE,
    STATEMENTS_FILE,
    LoadedPackage,
    load_builtin_package,
    load_package_from_texts,
)
from ket.kernel.config.statements.formula.parser import (
    AccountFunctionCall,
    BinaryOperation,
    FormulaFunction,
    parse_formula,
)
from ket.kernel.errors import ConfigPackageDataInvalidError

_MANIFEST = json.dumps(
    {
        "code": "STMT-TEST",
        "scheme": "TT99",
        "name": "Gói test layout",
        "version": 1,
        "effective_from": "2026-01-01",
        "effective_to": None,
    }
)
_ACCOUNTS = (
    "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
    "detail_tracking,is_locked\n"
    "111,Tiền mặt,,,0,0,0,,1\n"
    "331,Phải trả người bán,,,2,0,0,vendor,1\n"
)
_EMPTY_DEFAULTS = "document_type,purpose,account_code\n"
_EMPTY_PAIRS = "source_account,target_account,sequence,description\n"


def _load(statements: object) -> LoadedPackage:
    return load_package_from_texts(
        {
            PACKAGE_MANIFEST_FILE: _MANIFEST,
            ACCOUNTS_FILE: _ACCOUNTS,
            DEFAULT_ACCOUNTS_FILE: _EMPTY_DEFAULTS,
            CLOSING_PAIRS_FILE: _EMPTY_PAIRS,
            STATEMENTS_FILE: json.dumps(statements),
        }
    )


def _layout(rows: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": "T01",
        "name": "Layout test",
        "statement_kind": "balance_sheet",
        "rows": rows,
    }
    base.update(overrides)
    return base


def _row(row_code: str, formula: str, **extra: object) -> dict[str, object]:
    return {"row_code": row_code, "label": f"Chỉ tiêu {row_code}", "formula": formula, **extra}


class TestStatementsValidation:
    def test_missing_statements_file_is_valid_and_yields_no_layouts(self) -> None:
        loaded = load_package_from_texts(
            {
                PACKAGE_MANIFEST_FILE: _MANIFEST,
                ACCOUNTS_FILE: _ACCOUNTS,
                DEFAULT_ACCOUNTS_FILE: _EMPTY_DEFAULTS,
                CLOSING_PAIRS_FILE: _EMPTY_PAIRS,
            }
        )
        assert loaded.statements == ()

    def test_valid_layout_loads_with_display_order_from_file_order(self) -> None:
        loaded = _load(
            [_layout([_row("100", "DR(111)"), _row("300", "CR(331)"), _row("440", "[300]")])]
        )
        layout = loaded.statements[0]
        assert [(r.row_code, r.display_order) for r in layout.rows] == [
            ("100", 1),
            ("300", 2),
            ("440", 3),
        ]

    @pytest.mark.parametrize(
        "statements",
        [
            {"not": "a list"},
            [{"code": "T01", "name": "x", "statement_kind": "balance_sheet", "rows": []}],
            [_layout([_row("100", "DR(111)")], statement_kind="quarterly")],
            [_layout([_row("100", "DR(111)")]), _layout([_row("100", "DR(111)")])],
            [_layout([_row("100", "DR(111)"), _row("100", "DR(331)")])],
            [_layout([_row("10 0", "DR(111)")])],
            [_layout([_row("100", "DR(111) +")])],
            [_layout([_row("100", "[999]")])],
            [_layout([_row("100", "[200]"), _row("200", "[100]")])],
            [_layout([_row("100", "DR(642)")])],
            [_layout([_row("100", "DR(64*)")])],
            [_layout([_row("100", "DR(111)", indent_level=99)])],
            [_layout([_row("100", "DR(111)", is_bold="yes")])],
            [_layout([_row("100", "DR(111)")], code="B01/../DN")],
            [_layout([_row("100", "DR(111)")], code="B01 DN")],
            [_layout([_row("100", "DR(111)")], code="-B01")],
        ],
    )
    def test_invalid_statements_are_rejected_fail_closed(self, statements: object) -> None:
        with pytest.raises(ConfigPackageDataInvalidError):
            _load(statements)

    def test_error_carries_layout_and_row_context(self) -> None:
        with pytest.raises(ConfigPackageDataInvalidError) as caught:
            _load([_layout([_row("100", "DR(999)")])])
        assert caught.value.details.get("layout") == "T01"
        assert caught.value.details.get("row") == "100"


# Mã số + thứ tự chỉ tiêu của mẫu B01-DN (Phụ lục IV TT 99/2025/TT-BTC,
# `docs/TT99/verbatim/bctc-b01-bao-cao-tinh-hinh-tai-chinh-nam.md`).
_B01_EXPECTED_ROW_CODES = [
    "100",
    "110",
    "111",
    "112",
    "120",
    "121",
    "122",
    "123",
    "124",
    "125",
    "126",
    "130",
    "131",
    "132",
    "133",
    "134",
    "135",
    "136",
    "137",
    "140",
    "141",
    "142",
    "150",
    "151",
    "152",
    "153",
    "160",
    "161",
    "162",
    "163",
    "164",
    "165",
    "200",
    "210",
    "211",
    "212",
    "213",
    "214",
    "215",
    "216",
    "220",
    "221",
    "222",
    "223",
    "224",
    "225",
    "226",
    "227",
    "228",
    "229",
    "230",
    "231",
    "232",
    "233",
    "234",
    "235",
    "236",
    "237",
    "238",
    "240",
    "241",
    "242",
    "250",
    "251",
    "252",
    "260",
    "261",
    "262",
    "263",
    "264",
    "265",
    "266",
    "270",
    "271",
    "272",
    "273",
    "274",
    "280",
    "300",
    "310",
    "311",
    "312",
    "313",
    "314",
    "315",
    "316",
    "317",
    "318",
    "319",
    "320",
    "321",
    "322",
    "323",
    "324",
    "325",
    "330",
    "331",
    "332",
    "333",
    "334",
    "335",
    "336",
    "337",
    "338",
    "339",
    "340",
    "341",
    "342",
    "343",
    "344",
    "400",
    "411",
    "411a",
    "411b",
    "412",
    "413",
    "414",
    "415",
    "416",
    "417",
    "418",
    "419",
    "420",
    "420a",
    "420b",
    "440",
]

# Mẫu B02-DN (cùng nguồn, `bctc-b02-ket-qua-kinh-doanh-nam.md`).
_B02_EXPECTED_ROW_CODES = [
    "01",
    "02",
    "10",
    "11",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "30",
    "31",
    "32",
    "40",
    "50",
    "51",
    "52",
    "60",
    "70",
    "71",
]


class TestBuiltinTt99Statements:
    def test_b01_row_codes_match_the_circular_form_in_order(self) -> None:
        loaded = load_builtin_package("tt99")
        layouts = {layout.code: layout for layout in loaded.statements}
        b01 = layouts["B01-DN"]
        assert b01.statement_kind == "balance_sheet"
        assert [row.row_code for row in b01.rows] == _B01_EXPECTED_ROW_CODES

    def test_b02_row_codes_match_the_circular_form_in_order(self) -> None:
        loaded = load_builtin_package("tt99")
        layouts = {layout.code: layout for layout in loaded.statements}
        b02 = layouts["B02-DN"]
        assert b02.statement_kind == "income"
        assert [row.row_code for row in b02.rows] == _B02_EXPECTED_ROW_CODES

    def test_totals_follow_the_circular_identities(self) -> None:
        loaded = load_builtin_package("tt99")
        rows = {
            row.row_code: row.formula
            for layout in loaded.statements
            if layout.code == "B01-DN"
            for row in layout.rows
        }
        assert rows["280"] == "[100] + [200]"
        assert rows["440"] == "[300] + [400]"
        assert rows["100"] == "[110] + [120] + [130] + [140] + [150] + [160]"
        assert rows["300"] == "[310] + [330]"

    def test_starred_rows_never_add_positively_in_any_builtin_balance_sheet(self) -> None:
        """Chỉ tiêu (*) của mẫu ghi số âm — công thức phải là 0 hoặc 0 - ….
        Quét CẢ HAI gói builtin, mọi layout balance_sheet (lỗ phủ L-3 review 5B)."""
        checked = 0
        for slug in ("tt99", "tt133"):
            for layout in load_builtin_package(slug).statements:
                if layout.statement_kind != "balance_sheet":
                    continue
                for row in layout.rows:
                    if "(*)" not in row.label:
                        continue
                    checked += 1
                    assert row.formula == "0" or row.formula.startswith("0 - "), (
                        slug,
                        layout.code,
                        row.row_code,
                        row.formula,
                    )
        assert checked > 20, "quét phải chạm được các chỉ tiêu (*) thật, không rỗng"

    def test_income_layouts_use_turnover_functions_only(self) -> None:
        """B02 đo PHÁT SINH — hàm số dư tại thời điểm (DR/CR/BAL) trong layout
        income là dấu hiệu nhập nhầm cột (loại sai "Rất cao" của bảng rủi ro).
        Quét mọi layout income của cả hai gói builtin (lỗ phủ L-3 review 5B).
        So theo token đã parse, không so chuỗi: `DR_PS(` chứa `DR(` là false
        positive chờ sẵn của cách so chuỗi."""
        banned = {FormulaFunction.DR, FormulaFunction.CR, FormulaFunction.BAL}

        def functions_of(node: object) -> set[FormulaFunction]:
            if isinstance(node, AccountFunctionCall):
                return {node.function}
            if isinstance(node, BinaryOperation):
                return functions_of(node.left) | functions_of(node.right)
            return set()

        for slug in ("tt99", "tt133"):
            for layout in load_builtin_package(slug).statements:
                if layout.statement_kind != "income":
                    continue
                for row in layout.rows:
                    used = functions_of(parse_formula(row.formula))
                    assert not (used & banned), (slug, layout.code, row.row_code, row.formula)


class TestBuiltinTt133Statements:
    def test_layouts_exist_with_expected_kinds(self) -> None:
        loaded = load_builtin_package("tt133")
        kinds = {layout.code: layout.statement_kind for layout in loaded.statements}
        assert kinds == {"B01a-DNN": "balance_sheet", "B02-DNN": "income"}

    def test_total_identities(self) -> None:
        loaded = load_builtin_package("tt133")
        b01a = next(layout for layout in loaded.statements if layout.code == "B01a-DNN")
        rows = {row.row_code: row.formula for row in b01a.rows}
        assert rows["500"] == "[300] + [400]"
        assert "[110]" in rows["200"] and "[180]" in rows["200"]
