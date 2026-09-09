"""Nội dung bản thể hiện hóa đơn — lát 7E-3.

Bài đơn vị, không cần cơ sở dữ liệu: `_vat_rate_label` là hàm thuần, và nó
mang **hai con số của pháp luật** trên chứng từ thuế.

Vì sao tệp này tồn tại: vòng review pre-landing phát hiện toàn bộ khối nội dung
mới của bản in (cột thuế suất, khối tỷ giá/quy đổi) là **mã chết trong CI** —
bài đầu-cuối duy nhất chạy trên một hóa đơn VND thuế suất 10% và chỉ khẳng định
tệp trả về bắt đầu bằng `%PDF`. Hai lỗi số học đi lọt qua cả vòng review thứ
nhất vì thế.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ket.modules.einvoice.print_details import _NOT_DECLARED, _vat_rate_label


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        # `0%` là thuế suất THẬT (hàng xuất khẩu), không phải "chưa khai" — và
        # nó là ca mà bản đầu in ra một dấu `%` trơ trọi, vì `format_quantity`
        # trả chuỗi rỗng cho số 0 theo đúng thiết kế của cột số lượng.
        (Decimal(0), "0%"),
        (Decimal("0.00"), "0%"),
        (Decimal(5), "5%"),
        (Decimal(8), "8%"),
        (Decimal(10), "10%"),
        (Decimal("10.00"), "10%"),
        # Thuế suất lẻ: `VAT_RATE_SCALE = 2` nên API nhận được, và bản đầu
        # **làm tròn** chúng — `8,25%` in thành `8%`, `4,5%` thành `5%`.
        (Decimal("8.25"), "8,25%"),
        (Decimal("4.5"), "4,5%"),
        (Decimal("1.75"), "1,75%"),
    ],
)
def test_the_printed_vat_rate_is_neither_rounded_nor_blanked(rate: Decimal, expected: str) -> None:
    """Thuế suất in ra đúng con số đã khai, không làm tròn và không thành rỗng."""
    assert _vat_rate_label(rate) == expected


def test_an_undeclared_rate_is_not_printed_as_zero_percent() -> None:
    """`None` ≠ `0`, cùng luật mà bộ dựng XML của 7E-2 đã đặt.

    Hai thứ khác nhau về quyền khấu trừ đầu vào của người mua, nên in một dòng
    chưa xác định thành "thuế suất 0%" là lời khai thuế không có căn cứ.
    """
    assert _vat_rate_label(None) == _NOT_DECLARED
    assert _vat_rate_label(None) != _vat_rate_label(Decimal(0))


def test_a_whole_rate_never_prints_in_scientific_notation() -> None:
    """`Decimal("10").normalize()` là `1E+1`.

    Bẫy này đã một lần biến một triệu đồng thành `1E+6` trong bản XML ở 7E-2;
    `:f` là thứ giữ con số ở dạng thập phân, và bài này ghim nó.
    """
    for rate in (Decimal(10), Decimal(100), Decimal("20.0")):
        assert "E" not in _vat_rate_label(rate)
