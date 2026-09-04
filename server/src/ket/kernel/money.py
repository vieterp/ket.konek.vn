"""Phép tính tiền — `Decimal`, làm tròn nửa lên, không bao giờ dấu phẩy động.

Mục tiêu chất lượng số 1 của cả sản phẩm là **đúng số liệu** (plan.md §Overview,
FR-NFR-001/002). Hai quy tắc chống lệch:

1. **Không có dấu phẩy động trong mã nghiệp vụ.** Ép bằng
   `tests/test_no_float_in_domain.py` (quét AST cả tên `float` lẫn literal).
2. **Làm tròn tường minh, đúng một lần, ở cuối phép tính.** Kế toán VN dùng
   làm tròn nửa lên (`ROUND_HALF_UP`) — khác mặc định của Python là
   `ROUND_HALF_EVEN` (làm tròn nửa chẵn của IEEE). Dùng mặc định của Python
   là sai từ đồng đầu tiên: `Decimal("0.125")` ra `0.12` thay vì `0.13`.

`scale` mặc định là 2 (VND ghi sổ tới đồng vẫn dùng 2 chữ số cho nguyên tệ
lẻ). Từ phase 3 giá trị này đọc từ `settings_service` theo từng dữ liệu kế
toán (FR-SYS-064) và được truyền vào đây — module này cố ý **không** tự đọc
cấu hình để còn test được bằng bảng giá trị biên.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

MONEY_SCALE_DEFAULT: Final[int] = 2
"""Số chữ số thập phân mặc định của số tiền quy đổi (VND)."""

MONEY_SCALE_MAX: Final[int] = 6
"""Trần chữ số thập phân — chặn cấu hình vô lý làm hỏng chỉ số/so khớp."""

RATE_SCALE_DEFAULT: Final[int] = 6
"""Chữ số thập phân của tỷ giá (nguyên tệ → VND). Tỷ giá KHÔNG phải số tiền:
nó được nhân rồi mới làm tròn, nên giữ nhiều chữ số hơn."""

UNIT_PRICE_PRECISION: Final[int] = 24
UNIT_PRICE_SCALE: Final[int] = 6
"""Hình dạng cột của một **đơn giá** — trên dòng chứng từ cũng như trong bảng giá.

Cùng lý do với tỷ giá và khác `MONEY_SCALE_*`: đơn giá được **nhân** với số lượng
rồi thành tiền mới làm tròn, nên nó phải giữ nhiều chữ số hơn số tiền nó sinh ra.
Sáu số lẻ vì hàng nhập khẩu báo giá 0.0125 USD/cái là chuyện thường.

Ở kernel chứ không ở từng module: `item_price_levels` (danh mục) và dòng chứng từ
mua/bán phải cùng một hình dạng, nếu không thì lượt chép giá từ bảng giá xuống
dòng chứng từ là một phép làm tròn thầm lặng — và không ai đi tìm phép làm tròn
ở một lượt gán."""

ZERO: Final[Decimal] = Decimal(0)


def _validate_scale(scale: int) -> None:
    if scale < 0 or scale > MONEY_SCALE_MAX:
        raise ValueError(f"scale phải trong khoảng 0..{MONEY_SCALE_MAX}, nhận {scale}")


def _validate_decimal(value: Decimal, name: str) -> None:
    """Chặn kiểu sai ngay tại biên.

    mypy strict đã bắt phần lớn trường hợp lúc tĩnh, nhưng dữ liệu đến từ JSON,
    từ driver DB hay từ thư viện ngoài chỉ lộ ra lúc chạy — và một số nhị phân
    lọt vào đây thì sai số không còn dấu vết để truy.
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} phải là Decimal, nhận {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} phải là số hữu hạn, nhận {value}")


def round_money(value: Decimal, scale: int = MONEY_SCALE_DEFAULT) -> Decimal:
    """Làm tròn nửa lên về `scale` chữ số thập phân.

    Nửa lên = làm tròn **ra xa số 0**: `2.345 → 2.35` và `-2.345 → -2.35`.
    """
    _validate_decimal(value, "value")
    _validate_scale(scale)
    return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def multiply_money(amount: Decimal, factor: Decimal, scale: int = MONEY_SCALE_DEFAULT) -> Decimal:
    """Nhân rồi làm tròn **một lần** ở cuối (đơn giá × số lượng, gốc × tỷ giá)."""
    _validate_decimal(amount, "amount")
    _validate_decimal(factor, "factor")
    return round_money(amount * factor, scale)


def divide_money(amount: Decimal, divisor: Decimal, scale: int = MONEY_SCALE_DEFAULT) -> Decimal:
    """Chia rồi làm tròn một lần ở cuối.

    Chia cho 0 là lỗi lập trình (dữ liệu phải được chặn trước ở tầng nghiệp vụ),
    nên ném `ZeroDivisionError` chứ không trả 0 — trả 0 sẽ giấu lỗi vào sổ.
    """
    _validate_decimal(amount, "amount")
    _validate_decimal(divisor, "divisor")
    if divisor == ZERO:
        raise ZeroDivisionError("Không chia số tiền cho 0")
    return round_money(amount / divisor, scale)


def sum_money(values: Iterable[Decimal], scale: int = MONEY_SCALE_DEFAULT) -> Decimal:
    """Cộng dồn rồi làm tròn một lần ở cuối.

    Cộng từng phần đã làm tròn sẽ tích lũy sai lệch trên chứng từ nhiều dòng —
    đây là nguồn lệch cân đối kinh điển.
    """
    total = ZERO
    for index, value in enumerate(values):
        _validate_decimal(value, f"values[{index}]")
        total += value
    return round_money(total, scale)


def convert_currency(
    amount_fc: Decimal,
    rate: Decimal,
    scale: int = MONEY_SCALE_DEFAULT,
) -> Decimal:
    """Quy đổi nguyên tệ sang VND: `amount_fc × rate`, làm tròn ở cuối (LD-12).

    Tỷ giá âm hoặc bằng 0 là dữ liệu sai — chặn tại đây thay vì để nó chạy
    tiếp vào bút toán.
    """
    _validate_decimal(rate, "rate")
    if rate <= ZERO:
        raise ValueError(f"Tỷ giá phải dương, nhận {rate}")
    return multiply_money(amount_fc, rate, scale)
