"""Bất biến của số tiền ngoại tệ (N5, FR-NFR-032, FR-NFR-002).

`MoneyFc` tồn tại để một bộ ba (nguyên tệ, tỷ giá, quy đổi) **không nhất quán**
không dựng lên được. Test ở đây kiểm đúng điều đó, kể cả ở những chỗ dễ trượt
nhất: làm tròn nửa lên, số âm, và tỷ giá nhiều chữ số thập phân.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.kernel.currency.money_fc import MoneyFc


def test_converting_keeps_the_three_values_consistent() -> None:
    money = MoneyFc.convert(currency="USD", amount_fc=Decimal("100.00"), rate=Decimal("25400.5"))

    assert money.amount_fc == Decimal("100.00")
    assert money.rate == Decimal("25400.5")
    assert money.amount_base == Decimal("2540050.00")


def test_an_inconsistent_triple_cannot_be_constructed() -> None:
    """Dựng thẳng bằng `MoneyFc(...)` với số quy đổi sai phải nổ ngay.

    Đây là hàng rào cho phase sau: một đường ghi mới sửa tỷ giá rồi quên tính
    lại số quy đổi sẽ dừng ở đây thay vì đưa một con số sai vào bút toán.
    """
    with pytest.raises(ValueError, match="không khớp"):
        MoneyFc(
            currency="USD",
            amount_fc=Decimal("100"),
            rate=Decimal("25000"),
            amount_base=Decimal("2500"),
        )


def test_rounding_is_half_up_not_half_even() -> None:
    """Kế toán VN làm tròn nửa **lên**; mặc định của Python là nửa **chẵn**.

    `0.005 × 1 = 0.005` phải ra `0.01`, không phải `0.00`.
    """
    money = MoneyFc.convert(currency="USD", amount_fc=Decimal("0.005"), rate=Decimal("1"))
    assert money.amount_base == Decimal("0.01")


def test_negative_amounts_round_away_from_zero() -> None:
    money = MoneyFc.convert(currency="USD", amount_fc=Decimal("-0.005"), rate=Decimal("1"))
    assert money.amount_base == Decimal("-0.01")


def test_there_is_no_shortcut_that_hands_out_a_rate_of_one() -> None:
    """Chỉ `ExchangeRateService.resolve` được phép trả tỷ giá 1 (sửa sau review C1).

    Một `MoneyFc.base(currency=..., amount=...)` nhận mã tiền nào cũng được sẽ
    biến 5.000 USD thành 5.000 VND mà không lỗi, không vết. Test này canh cho
    lối tắt đó không quay lại — nó rất dễ được thêm lại vì trông tiện.
    """
    assert not hasattr(MoneyFc, "base")


def test_a_non_positive_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="Tỷ giá"):
        MoneyFc.convert(currency="USD", amount_fc=Decimal("1"), rate=Decimal("0"))


def test_the_value_is_immutable() -> None:
    money = MoneyFc.convert(currency="VND", amount_fc=Decimal("1"), rate=Decimal("1"))
    with pytest.raises(AttributeError):
        money.rate = Decimal("2")  # type: ignore[misc] - đang kiểm chính tính bất biến
