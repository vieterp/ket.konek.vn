"""Grammar + evaluator công thức chỉ tiêu BCTC (FR-GLE-043, lát 5B) — không cần DB.

Ba nhóm bất biến:

* **Parser đúng grammar và chỉ grammar**: dạng hợp lệ parse ra đúng cây; ký tự
  ngoài grammar, hàm lạ, dải lệch độ dài, `eval`-payload — tất cả là
  `StatementFormulaInvalidError`, không bao giờ là thực thi.
* **Dải TK khớp tiền tố**: `128` phủ cả nhánh `1281`; `131..138` phủ `1311`
  nhưng không phủ `141`; hai cận phải cùng độ dài, thuận chiều.
* **Evaluator**: bảy hàm đúng ngữ nghĩa từng bên; rowref chạy theo tô-pô;
  chu trình và rowref lạ nổ thành lỗi cấu hình rõ ràng.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.kernel.config.statements.formula.account_range import make_range
from ket.kernel.config.statements.formula.evaluator import AccountAmounts, evaluate_rows
from ket.kernel.config.statements.formula.parser import (
    AccountFunctionCall,
    BinaryOperation,
    FormulaFunction,
    NumberLiteral,
    RowReference,
    account_ranges_of,
    parse_formula,
    row_references_of,
)
from ket.kernel.errors import StatementFormulaInvalidError


class TestParser:
    def test_parses_the_documented_example_formula(self) -> None:
        node = parse_formula("DR(111*) + DR(112) + DR(1281) + DR(1288)")
        ranges = [r.spec() for r in account_ranges_of(node)]
        assert ranges == ["111", "112", "1281", "1288"]

    def test_rowrefs_and_numbers_combine_left_associatively(self) -> None:
        node = parse_formula("[20] + [21] - [23] - [25]")
        # ((([20] + [21]) - [23]) - [25]) — trừ phải kết trái, nếu không
        # [20]+[21]-([23]-[25]) đổi dấu [25] và BCTC sai lặng lẽ.
        assert isinstance(node, BinaryOperation)
        assert node.operator == "-"
        assert isinstance(node.right, RowReference) and node.right.row_code == "25"
        assert row_references_of(node) == frozenset({"20", "21", "23", "25"})

    def test_leading_zero_minus_gives_negative_rows(self) -> None:
        node = parse_formula("0 - CR(2294)")
        assert isinstance(node, BinaryOperation)
        assert isinstance(node.left, NumberLiteral)
        call = node.right
        assert isinstance(call, AccountFunctionCall)
        assert call.function is FormulaFunction.CR

    def test_multi_range_function_and_numeric_range(self) -> None:
        node = parse_formula("BAL(131..138, 141)")
        assert isinstance(node, AccountFunctionCall)
        assert [r.spec() for r in node.ranges] == ["131..138", "141"]

    @pytest.mark.parametrize(
        "formula",
        [
            "",
            "   ",
            "DR()",
            "DR(111",
            "SUM(111)",
            "DR(111) * 2",
            "[100",
            "[]",
            "[10 0]",
            "DR(111) DR(112)",
            "131..138",
            "DR(131..)",
            "DR(..138)",
            "1.5",
            "__import__('os')",
            "{{''.__class__}}",
        ],
    )
    def test_everything_outside_the_grammar_is_rejected(self, formula: str) -> None:
        with pytest.raises(StatementFormulaInvalidError):
            parse_formula(formula)

    def test_range_bounds_must_share_length_and_order(self) -> None:
        with pytest.raises(StatementFormulaInvalidError):
            parse_formula("DR(131..1381)")
        with pytest.raises(StatementFormulaInvalidError):
            parse_formula("DR(138..131)")


class TestAccountRange:
    def test_plain_code_matches_its_whole_subtree_by_prefix(self) -> None:
        rng = make_range("128", None)
        assert rng.matches("128")
        assert rng.matches("1281")
        assert not rng.matches("12")
        assert not rng.matches("129")

    def test_numeric_range_compares_the_prefix_of_equal_length(self) -> None:
        rng = make_range("131", "138")
        assert rng.matches("131")
        assert rng.matches("1311")
        assert rng.matches("138")
        assert rng.matches("1388")
        assert not rng.matches("141")
        assert not rng.matches("13")


def _amounts() -> dict[str, AccountAmounts]:
    return {
        # 111 dư Nợ 800; phát sinh Nợ 1000 / Có 200.
        "111": AccountAmounts(
            balance_debit=Decimal(800),
            turnover_debit=Decimal(1000),
            turnover_credit=Decimal(200),
        ),
        # 131 dư Có 300 (ứng trước của khách, sau khi net ở mức TK).
        "131": AccountAmounts(balance_credit=Decimal(300)),
        # 511 không dư; phát sinh Có 500 / Nợ 40 (giảm trừ).
        "511": AccountAmounts(turnover_debit=Decimal(40), turnover_credit=Decimal(500)),
    }


class TestEvaluator:
    @pytest.mark.parametrize(
        ("formula", "expected"),
        [
            ("DR(111)", Decimal(800)),
            ("CR(111)", Decimal(0)),
            ("DR(131)", Decimal(0)),
            ("CR(131)", Decimal(300)),
            ("BAL(111, 131)", Decimal(500)),
            ("DR_PS(111)", Decimal(1000)),
            ("CR_PS(511)", Decimal(500)),
            ("DR_NET(111)", Decimal(800)),
            ("CR_NET(511)", Decimal(460)),
            ("DR_NET(511)", Decimal(-460)),
            ("0 - CR(131)", Decimal(-300)),
            ("DR(11*) + 7", Decimal(807)),
            ("DR(999)", Decimal(0)),
        ],
    )
    def test_function_semantics(self, formula: str, expected: Decimal) -> None:
        values = evaluate_rows({"X": parse_formula(formula)}, _amounts())
        assert values["X"] == expected

    def test_rowrefs_resolve_in_topological_order_regardless_of_layout_order(self) -> None:
        formulas = {
            "440": parse_formula("[300] + [400]"),
            "400": parse_formula("CR(131)"),
            "300": parse_formula("DR(111)"),
        }
        values = evaluate_rows(formulas, _amounts())
        assert values["440"] == Decimal(1100)

    def test_unknown_rowref_is_a_config_error(self) -> None:
        with pytest.raises(StatementFormulaInvalidError):
            evaluate_rows({"100": parse_formula("[999]")}, _amounts())

    def test_cycles_are_detected(self) -> None:
        formulas = {
            "100": parse_formula("[200] + 1"),
            "200": parse_formula("[100] - 1"),
        }
        with pytest.raises(StatementFormulaInvalidError):
            evaluate_rows(formulas, _amounts())

    def test_empty_amounts_evaluate_every_row_to_its_constant_part(self) -> None:
        values = evaluate_rows({"100": parse_formula("DR(111) + 5")}, {})
        assert values["100"] == Decimal(5)
