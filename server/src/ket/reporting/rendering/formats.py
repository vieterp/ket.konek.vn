"""Đổi giá trị ô thành chữ cho bản in PDF (quy ước số Việt Nam).

Chỉ TRÌNH BÀY — mọi phép cộng đã xong ở `grouping.py` bằng `Decimal`. XLSX
không đi qua đây: nó giữ giá trị số + `num_format` để Excel tự định dạng
(FR-RPT-012), người dùng kiểm lại được bằng công thức.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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


def format_quantity(value: Decimal | None) -> str:
    return format_money(value, blank_zero=True)


def format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value is not None else ""


def format_text(value: object) -> str:
    return "" if value is None else str(value)
