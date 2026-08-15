"""Bảng giá trị biên cho phép tính tiền (FR-NFR-001/002).

Mỗi dòng trong bảng dưới đây là một cách phần mềm kế toán lệch số trong thực
tế. Chúng được viết ra thành test vì đọc lại code không phát hiện được: cả
`ROUND_HALF_EVEN` lẫn `ROUND_HALF_UP` đều "trông đúng".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from konek.kernel.money import (
    MONEY_SCALE_DEFAULT,
    convert_currency,
    divide_money,
    multiply_money,
    round_money,
    sum_money,
)


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        # Nửa lên, không phải nửa chẵn: mặc định của Python cho 0.125 ra 0.12.
        ("0.125", 2, "0.13"),
        ("0.135", 2, "0.14"),
        ("2.345", 2, "2.35"),
        # Số âm làm tròn **ra xa số 0** — nếu không, một khoản giảm trừ và
        # khoản tăng cùng độ lớn sẽ không triệt tiêu nhau.
        ("-0.125", 2, "-0.13"),
        ("-2.345", 2, "-2.35"),
        # Đã đúng số chữ số thì giữ nguyên, kể cả số rất lớn (nghìn tỷ đồng).
        ("1234567890123.45", 2, "1234567890123.45"),
        # VND ghi sổ tới đồng.
        ("1500.5", 0, "1501"),
        ("-1500.5", 0, "-1501"),
        ("1499.4999", 0, "1499"),
        # Tỷ giá giữ nhiều chữ số hơn số tiền.
        ("24567.891234", 6, "24567.891234"),
        ("0", 2, "0.00"),
    ],
)
def test_round_money_boundary_table(value: str, scale: int, expected: str) -> None:
    assert round_money(Decimal(value), scale) == Decimal(expected)


def test_round_money_rejects_non_decimal() -> None:
    """Số nhị phân lọt vào đường tính tiền là lỗi im lặng — chặn ngay tại biên."""
    with pytest.raises(TypeError):
        round_money("1.5")  # type: ignore[arg-type]


def test_round_money_rejects_absurd_scale() -> None:
    with pytest.raises(ValueError, match="scale"):
        round_money(Decimal("1.5"), 99)


def test_multiply_rounds_once_at_the_end() -> None:
    """Đơn giá × số lượng: làm tròn một lần ở cuối, không làm tròn từng bước."""
    unit_price = Decimal("1234.567")
    quantity = Decimal("3")
    assert multiply_money(unit_price, quantity) == Decimal("3703.70")
    # Nếu làm tròn đơn giá trước rồi mới nhân thì ra 3703.71 — lệch 1 xu mỗi
    # dòng, và một chứng từ 500 dòng lệch tới mức không đối chiếu được.
    assert round_money(unit_price) * quantity == Decimal("3703.71")


def test_sum_rounds_once_at_the_end() -> None:
    """Tổng của các số chưa làm tròn ≠ tổng của các số đã làm tròn."""
    values = [Decimal("0.005")] * 4
    assert sum_money(values) == Decimal("0.02")
    assert sum(round_money(value) for value in values) == Decimal("0.04")


def test_divide_by_zero_is_an_error_not_zero() -> None:
    """Trả 0 khi chia cho 0 sẽ giấu lỗi dữ liệu vào tận sổ cái."""
    with pytest.raises(ZeroDivisionError):
        divide_money(Decimal("100"), Decimal("0"))


def test_divide_allocates_with_rounding() -> None:
    assert divide_money(Decimal("100"), Decimal("3")) == Decimal("33.33")


def test_convert_currency_requires_positive_rate() -> None:
    with pytest.raises(ValueError, match="Tỷ giá"):
        convert_currency(Decimal("100"), Decimal("0"))


def test_convert_currency_uses_full_rate_precision() -> None:
    """Quy đổi dùng nguyên độ chính xác của tỷ giá rồi mới làm tròn (LD-12)."""
    assert convert_currency(Decimal("100"), Decimal("24567.891234")) == Decimal("2456789.12")


def test_default_scale_is_two() -> None:
    assert MONEY_SCALE_DEFAULT == 2
