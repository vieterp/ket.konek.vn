"""Phân bổ chi phí mua hàng — các trường hợp biên và lỗi (SRS 05 §4.2).

Mở rộng `test_landed_cost_allocation.py` với những trường hợp không xảy ra
thường xuyên nhưng phải được xử lý đúng: tất cả trọng số = 0, công thức
tính thặng dư với số âm, lỗi code không biết, scale khác nhau.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.kernel.errors import PostingValidationError
from ket.modules.purchase.landed_cost import (
    ALLOCATION_METHOD_UNKNOWN_CODE,
    ALLOCATION_NO_BASIS_CODE,
    AllocationLine,
    allocate,
)
from ket.modules.purchase.models import LandedCostAllocation

_ZERO = Decimal(0)


def _line(amount: str, quantity: str | None = None, manual: str = "0") -> AllocationLine:
    """Xây dựng dòng phân bổ để dùng trong test."""
    return AllocationLine(
        amount_fc=Decimal(amount),
        quantity=Decimal(quantity) if quantity is not None else None,
        manual_fc=Decimal(manual),
    )


class TestByQuantityEdgeCases:
    """Phân bổ theo số lượng — các trường hợp biên."""

    def test_all_zero_quantities_fails(self) -> None:
        """Phân bổ theo số lượng nhưng tất cả dòng = 0 → không có cơ sở."""
        with pytest.raises(PostingValidationError) as excinfo:
            allocate(
                method=LandedCostAllocation.BY_QUANTITY,
                total_fc=Decimal(100),
                lines=(_line("100", "0"), _line("100", "0")),
            )
        assert excinfo.value.violations[0].code == ALLOCATION_NO_BASIS_CODE

    def test_mix_of_none_and_zero_quantities_fails(self) -> None:
        """Một dòng None, một dòng 0 → tổng cơ sở = 0 → lỗi."""
        with pytest.raises(PostingValidationError) as excinfo:
            allocate(
                method=LandedCostAllocation.BY_QUANTITY,
                total_fc=Decimal(100),
                lines=(_line("100"), _line("100", "0")),
            )
        assert excinfo.value.violations[0].code == ALLOCATION_NO_BASIS_CODE

    def test_one_nonzero_quantity_among_nones(self) -> None:
        """Một dòng có số lượng, các dòng khác None → phân bổ chỉ vào dòng có số."""
        shares = allocate(
            method=LandedCostAllocation.BY_QUANTITY,
            total_fc=Decimal(100),
            lines=(_line("1000"), _line("10", "2"), _line("100")),
        )
        assert shares == (Decimal(0), Decimal(100), Decimal(0))

    def test_by_quantity_with_three_lines_remainder(self) -> None:
        """Phân bổ theo số lượng với 3 dòng, có phần dư."""
        shares = allocate(
            method=LandedCostAllocation.BY_QUANTITY,
            total_fc=Decimal(100),
            lines=(_line("100", "1"), _line("100", "2"), _line("100", "3")),
        )
        # 100 / 6 = 16.666... → 16.67 + 16.67 + 16.67 = 50.01 (lẻ 0.01)
        # phần lẻ dồn vào dòng 3 (trọng số lớn nhất = 3)
        assert shares == (Decimal("16.67"), Decimal("33.33"), Decimal("50.00"))
        assert sum(shares) == Decimal(100)


class TestByValueEdgeCases:
    """Phân bổ theo giá trị — các trường hợp biên."""

    def test_some_zero_amounts_by_value(self) -> None:
        """Phân bổ theo giá trị, một dòng = 0 → không nhận phần nào."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(100),
            lines=(_line("100"), _line("0"), _line("100")),
        )
        # Cơ sở = 200, dòng 1 = 50, dòng 2 = 0, dòng 3 = 50
        assert shares == (Decimal(50), Decimal(0), Decimal(50))
        assert sum(shares) == Decimal(100)

    def test_all_zero_amounts_by_value(self) -> None:
        """Phân bổ theo giá trị nhưng tất cả dòng = 0."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(0),
            lines=(_line("0"), _line("0"), _line("0")),
        )
        assert shares == (Decimal(0), Decimal(0), Decimal(0))

    def test_four_lines_by_value_remainder(self) -> None:
        """Phân bổ 1000 vào 4 dòng theo giá trị."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(1000),
            lines=(
                _line("250"),
                _line("250"),
                _line("250"),
                _line("250"),
            ),
        )
        # Bằng nhau: 1000 / 4 = 250 — không có dư, phân bổ đều.
        assert shares == (Decimal(250), Decimal(250), Decimal(250), Decimal(250))

    def test_remainder_distribution_to_first_largest(self) -> None:
        """Khi có dư, phần dư dồn vào dòng lớn nhất. Nếu bằng nhau thì dòng đầu."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(100),
            lines=(_line("100"), _line("100"), _line("100")),
        )
        # 100 / 3 = 33.33... → 33.34 + 33.33 + 33.33 (dòng 1 gánh phần lẻ)
        assert shares == (Decimal("33.34"), Decimal("33.33"), Decimal("33.33"))
        assert sum(shares) == Decimal(100)


class TestManualAllocationEdgeCases:
    """Phân bổ thủ công — các trường hợp biên."""

    def test_manual_with_fractional_shares(self) -> None:
        """Phân bổ thủ công với tiền lẻ."""
        lines = (
            _line("100", manual="33.33"),
            _line("100", manual="33.33"),
            _line("100", manual="33.34"),
        )
        shares = allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal("100"), lines=lines)
        assert shares == (Decimal("33.33"), Decimal("33.33"), Decimal("33.34"))

    def test_manual_with_unequal_lines(self) -> None:
        """Phân bổ thủ công không phụ thuộc vào giá trị của các dòng."""
        lines = (
            _line("1000", manual="60"),
            _line("10", manual="40"),
        )
        shares = allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(100), lines=lines)
        assert shares == (Decimal(60), Decimal(40))

    def test_manual_total_mismatch_high(self) -> None:
        """Phân bổ thủ công: tổng nhập > tổng chi phí → lỗi."""
        lines = (_line("100", manual="70"), _line("100", manual="40"))  # 110 != 100
        with pytest.raises(PostingValidationError) as excinfo:
            allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(100), lines=lines)
        assert excinfo.value.violations[0].code == "purchase.landed_cost_allocation_mismatch"

    def test_manual_total_mismatch_low(self) -> None:
        """Phân bổ thủ công: tổng nhập < tổng chi phí → lỗi."""
        lines = (_line("100", manual="40"), _line("100", manual="30"))  # 70 != 100
        with pytest.raises(PostingValidationError) as excinfo:
            allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(100), lines=lines)
        assert excinfo.value.violations[0].code == "purchase.landed_cost_allocation_mismatch"

    def test_manual_with_zero_shares(self) -> None:
        """Phân bổ thủ công: một dòng 0 là hợp lệ."""
        lines = (_line("100", manual="0"), _line("100", manual="100"))
        shares = allocate(method=LandedCostAllocation.MANUAL, total_fc=Decimal(100), lines=lines)
        assert shares == (Decimal(0), Decimal(100))


class TestAllocationMethodValidation:
    """Kiểm kiểu code phân bổ."""

    def test_unknown_method_code_raises(self) -> None:
        """Code phân bổ không tồn tại → lỗi."""
        with pytest.raises(PostingValidationError) as excinfo:
            allocate(
                method=999,  # Unknown!
                total_fc=Decimal(100),
                lines=(_line("100"), _line("100")),
            )
        assert excinfo.value.violations[0].code == ALLOCATION_METHOD_UNKNOWN_CODE


class TestScaleParameter:
    """Kiểm scale làm tròn."""

    def test_custom_scale_higher_precision(self) -> None:
        """Scale cao hơn (3 số lẻ thay vì 2)."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal("100.00"),
            lines=(_line("100"), _line("100"), _line("100")),
            scale=3,
        )
        # 100 / 3 = 33.333...
        assert sum(shares) == Decimal("100.000")
        # Tổng phải đúng dù làm tròn ở scale 3
        assert all(s.as_tuple().exponent <= -3 for s in shares)

    def test_scale_zero_integer_division(self) -> None:
        """Scale = 0 (làm tròn thành số nguyên)."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(100),
            lines=(_line("100"), _line("100"), _line("100")),
            scale=0,
        )
        assert sum(shares) == Decimal(100)
        # Tất cả phải là số nguyên
        assert all(s % 1 == 0 for s in shares)


class TestEdgeCasesCombined:
    """Kết hợp nhiều điều kiện biên."""

    def test_empty_lines_sequence(self) -> None:
        """Danh sách dòng rỗng → trả dãy dòng rỗng."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(100),
            lines=(),
        )
        assert shares == ()

    def test_single_line_gets_all(self) -> None:
        """Chỉ một dòng → nhận tất cả chi phí."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal(100),
            lines=(_line("1000"),),
        )
        assert shares == (Decimal(100),)

    def test_very_large_amount(self) -> None:
        """Phân bổ số tiền lớn."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal("999999999999.99"),
            lines=(_line("1000000"), _line("1000000")),
        )
        assert sum(shares) == Decimal("999999999999.99")

    def test_very_small_fractional_amount(self) -> None:
        """Phân bổ số tiền rất nhỏ."""
        shares = allocate(
            method=LandedCostAllocation.BY_VALUE,
            total_fc=Decimal("0.01"),
            lines=(_line("1"), _line("1")),
        )
        assert sum(shares) == Decimal("0.01")
