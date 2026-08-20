"""Đổi giá trị ô thành chữ cho bản in PDF (quy ước số Việt Nam).

Chỉ TRÌNH BÀY — mọi phép cộng đã xong ở `grouping.py` bằng `Decimal`. XLSX
không đi qua đây: nó giữ giá trị số + `num_format` để Excel tự định dạng
(FR-RPT-012), người dùng kiểm lại được bằng công thức.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_ZERO = Decimal(0)


def format_money(value: Decimal | None, *, blank_zero: bool) -> str:
    """`1234567.00` → `1.234.567`; phần lẻ chỉ hiện khi thật sự có.

    `blank_zero=True` cho ô dữ liệu (dòng phát sinh chỉ có một bên Nợ/Có — bên
    0 để trống theo quy ước sổ sách); dòng tổng truyền `False` vì "tổng bằng 0"
    là một khẳng định phải in ra.
    """
    if value is None:
        return ""
    if blank_zero and value == _ZERO:
        return ""
    sign = "-" if value < 0 else ""
    magnitude = -value if value < 0 else value
    integral = int(magnitude)
    fraction = (magnitude - integral).normalize()
    grouped = f"{integral:,}".replace(",", ".")
    if fraction == _ZERO:
        return f"{sign}{grouped}"
    digits = f"{fraction:f}"[2:]  # "0.5" → "5"
    return f"{sign}{grouped},{digits}"


def format_quantity(value: Decimal | None, *, decimals: int = 2) -> str:
    """Số lượng hiển thị với ĐÚNG `decimals` chữ số thập phân (FR-RPT-012).

    Khác `format_money` (phần lẻ chỉ hiện khi có): cột số lượng canh phải phải
    thẳng hàng dấu phẩy — `5` và `5,25` trong cùng cột in thành `5,00`/`5,25`.
    Chỉ TRÌNH BÀY: giá trị gốc không bị làm tròn ở đâu ngoài chuỗi in ra.
    """
    if value is None or value == _ZERO:
        return ""
    # HALF_UP chứ không banker's rounding mặc định (review 5D, L2): đây là
    # quy ước HIỂN THỊ quen mắt kế toán VN — giá trị gốc không bị làm tròn ở
    # đâu ngoài chuỗi in ra.
    quantized = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    magnitude = -quantized if quantized < 0 else quantized
    integral = int(magnitude)
    grouped = f"{integral:,}".replace(",", ".")
    if decimals == 0:
        return f"{sign}{grouped}"
    digits = f"{magnitude:f}".split(".")[1] if "." in f"{magnitude:f}" else ""
    digits = digits.ljust(decimals, "0")[:decimals]
    return f"{sign}{grouped},{digits}"


def format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value is not None else ""


def format_text(value: object) -> str:
    return "" if value is None else str(value)
