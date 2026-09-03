"""Phân bổ chi phí mua hàng (SRS 05 §4.2) — thuần, không cần DB.

Ba cách phân bổ: theo giá trị, theo số lượng, thủ công. Bất biến chung: tổng
phần đã chia bằng đúng tổng chi phí (không rơi một đồng nào vì làm tròn), phần
lẻ dồn vào dòng có trọng số lớn nhất.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.kernel.errors import PostingValidationError
from ket.modules.purchase.landed_cost import (
    ALLOCATION_MISMATCH_CODE,
    ALLOCATION_NO_BASIS_CODE,
    AllocationLine,
    CostLinePiece,
    allocate,
    pair_costs_with_lines,
)
from ket.modules.purchase.models import LandedCostAllocation


def _line(amount: str, quantity: str | None = None, manual: str = "0") -> AllocationLine:
    return AllocationLine(
        amount_fc=Decimal(amount),
        quantity=Decimal(quantity) if quantity is not None else None,
        manual_fc=Decimal(manual),
    )


def test_by_value_splits_in_proportion_and_keeps_the_total() -> None:
    shares = allocate(
        method=LandedCostAllocation.BY_VALUE,
        total_fc=Decimal(100),
        lines=(_line("100"), _line("100"), _line("100")),
    )
    # 100 / 3 không chia hết: hai dòng 33.33, dòng đầu (trọng số bằng nhau,
    # đứng trước) gánh phần lẻ 0.01.
    assert shares == (Decimal("33.34"), Decimal("33.33"), Decimal("33.33"))
    assert sum(shares) == Decimal(100)


def test_by_value_remainder_goes_to_the_heaviest_line() -> None:
    shares = allocate(
        method=LandedCostAllocation.BY_VALUE,
        total_fc=Decimal("1000"),
        lines=(_line("100"), _line("500"), _line("100")),
    )
    assert shares == (Decimal("142.86"), Decimal("714.28"), Decimal("142.86"))
    assert sum(shares) == Decimal(1000)


def test_by_quantity_uses_quantities_as_weights() -> None:
    shares = allocate(
        method=LandedCostAllocation.BY_QUANTITY,
        total_fc=Decimal(90),
        lines=(_line("1000", "1"), _line("10", "2")),
    )
    assert shares == (Decimal(30), Decimal(60))


def test_by_quantity_without_any_quantity_has_no_basis() -> None:
    with pytest.raises(PostingValidationError) as excinfo:
        allocate(
            method=LandedCostAllocation.BY_QUANTITY,
            total_fc=Decimal(90),
            lines=(_line("1000"), _line("10")),
        )
    assert excinfo.value.violations[0].code == ALLOCATION_NO_BASIS_CODE


def test_zero_total_allocates_zero_everywhere() -> None:
    shares = allocate(
        method=LandedCostAllocation.BY_VALUE, total_fc=Decimal(0), lines=(_line("0"), _line("0"))
    )
    assert shares == (Decimal(0), Decimal(0))


def test_manual_shares_must_add_up_to_the_total() -> None:
    lines = (_line("100", manual="70"), _line("100", manual="30"))
    assert allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(100), lines=lines) == (
        Decimal(70),
        Decimal(30),
    )
    with pytest.raises(PostingValidationError) as excinfo:
        allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(99), lines=lines)
    assert excinfo.value.violations[0].code == ALLOCATION_MISMATCH_CODE


def _d(value: str) -> Decimal:
    return Decimal(value)


def test_pairing_walks_costs_and_lines_in_order_without_rounding() -> None:
    """Hai khoản (60, 40) đổ vào ba phần phân bổ (50, 30, 20): mẩu nào cũng là
    `min` hai phần còn lại, tổng theo khoản và theo dòng đều nguyên vẹn."""
    pieces = pair_costs_with_lines([_d("60"), _d("40")], [_d("50"), _d("30"), _d("20")])
    assert pieces == (
        CostLinePiece(cost_index=0, line_index=0, amount_fc=_d("50")),
        CostLinePiece(cost_index=0, line_index=1, amount_fc=_d("10")),
        CostLinePiece(cost_index=1, line_index=1, amount_fc=_d("20")),
        CostLinePiece(cost_index=1, line_index=2, amount_fc=_d("20")),
    )
    by_cost: dict[int, Decimal] = {}
    by_line: dict[int, Decimal] = {}
    for piece in pieces:
        by_cost[piece.cost_index] = by_cost.get(piece.cost_index, Decimal(0)) + piece.amount_fc
        by_line[piece.line_index] = by_line.get(piece.line_index, Decimal(0)) + piece.amount_fc
    assert by_cost == {0: _d("60"), 1: _d("40")}
    assert by_line == {0: _d("50"), 1: _d("30"), 2: _d("20")}


def test_pairing_skips_zero_costs_and_zero_shares() -> None:
    """Khoản chỉ có thuế (`amount_fc = 0`) và dòng không nhận phần phân bổ
    không sinh mẩu nào — chỉ số vẫn trỏ đúng vị trí gốc."""
    pieces = pair_costs_with_lines([_d("0"), _d("100")], [_d("0"), _d("100"), _d("0")])
    assert pieces == (CostLinePiece(cost_index=1, line_index=1, amount_fc=_d("100")),)
    assert pair_costs_with_lines([], []) == ()


def test_pairing_refuses_mismatched_totals() -> None:
    with pytest.raises(RuntimeError):
        pair_costs_with_lines([_d("100")], [_d("50"), _d("49.99")])
