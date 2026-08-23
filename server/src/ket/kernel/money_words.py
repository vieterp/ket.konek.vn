"""Đọc số tiền thành chữ tiếng Việt — mẫu 01-TT/02-TT bắt buộc có dòng này.

"Số tiền: ......... (Viết bằng chữ): ........." là một dòng của chính biểu mẫu
chứng từ (TT99 Phụ lục I), không phải trang trí: chữ là thứ chặn sửa số trên
tờ phiếu giấy sau khi ký. Vì thế hàm ở đây **không làm tròn** — phần lẻ có thì
đọc ra, chứ không lặng lẽ bỏ đi cho câu chữ đẹp.

Quy ước đã chọn (ghi rõ vì tiếng Việt có nhiều lối đọc đều đúng):

* ``lẻ`` chứ không ``linh`` — lối phổ biến trên chứng từ kế toán miền Nam và
  trong phần mềm kế toán VN;
* ``bốn`` chứ không ``tư`` ở hàng đơn vị (24 = "hai mươi bốn") — cùng lý do
  dễ đối chiếu với chữ số, không phụ thuộc vùng miền;
* giữ ``mốt`` (21) và ``lăm`` (15, 25) vì hai từ này gần như thống nhất toàn
  quốc, đọc "hai mươi một"/"hai mươi năm" nghe sai hơn là khác vùng;
* phần thập phân đọc sau chữ ``phẩy``, số 0 đứng đầu đọc thành ``không``
  (1234,05 → "… phẩy không năm"); số 0 ở CUỐI bỏ đi vì nó không đổi giá trị
  (1234,50 → "… phẩy năm"), câu chữ bám giá trị chứ không bám cách ghi.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

_DIGITS: Final = (
    "không",
    "một",
    "hai",
    "ba",
    "bốn",
    "năm",
    "sáu",
    "bảy",
    "tám",
    "chín",
)

_SCALES: Final = ("", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ", "tỷ tỷ")
"""Tên bậc cho từng nhóm ba chữ số. Trên ``tỷ`` tiếng Việt ghép lại
("nghìn tỷ", "triệu tỷ") chứ không có từ riêng — bảng dừng ở 10^20, quá tầm
mọi số tiền mà `AMOUNT_PRECISION` cho phép lưu."""

_ZERO: Final = Decimal(0)


def _read_group(value: int, *, padded: bool) -> str:
    """Một nhóm ba chữ số (0..999) thành chữ.

    `padded=True` là nhóm ĐỨNG SAU một nhóm đã đọc: phải đọc đủ cả hàng trăm
    rỗng ("một triệu **không trăm lẻ năm**") vì bỏ đi thì "1.000.005" và
    "1.000.500" đọc giống hệt nhau.
    """
    hundreds, rest = divmod(value, 100)
    tens, units = divmod(rest, 10)
    parts: list[str] = []
    if hundreds or padded:
        parts += [_DIGITS[hundreds], "trăm"]
    if tens == 0:
        if units:
            if hundreds or padded:
                parts.append("lẻ")
            parts.append(_DIGITS[units])
    elif tens == 1:
        parts.append("mười")
        if units == 5:
            parts.append("lăm")
        elif units:
            parts.append(_DIGITS[units])
    else:
        parts += [_DIGITS[tens], "mươi"]
        if units == 1:
            parts.append("mốt")
        elif units == 5:
            parts.append("lăm")
        elif units:
            parts.append(_DIGITS[units])
    return " ".join(parts)


def _read_integer(value: int) -> str:
    """Phần nguyên thành chữ — nhóm ba chữ số bằng 0 thì bỏ cả tên bậc."""
    if value == 0:
        return _DIGITS[0]
    groups: list[int] = []
    while value:
        value, group = divmod(value, 1000)
        groups.append(group)
    if len(groups) > len(_SCALES):
        raise ValueError("Số tiền vượt quá tầm đọc thành chữ")
    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            continue
        words.append(_read_group(group, padded=bool(words)))
        if _SCALES[index]:
            words.append(_SCALES[index])
    return " ".join(words)


def _read_fraction(digits: str) -> str:
    """Chữ số sau dấu phẩy: số 0 đứng đầu đọc rời, phần còn lại đọc như một số.

    "50" → "năm mươi"; "05" → "không năm"; "005" → "không không năm".
    """
    leading = len(digits) - len(digits.lstrip("0"))
    words = ["không"] * leading
    remainder = digits[leading:]
    if remainder:
        words.append(_read_integer(int(remainder)))
    return " ".join(words)


def amount_in_words(value: Decimal, *, unit: str = "đồng") -> str:
    """`Decimal` → câu chữ hoa đầu câu cho dòng "(Viết bằng chữ)" của chứng từ.

    `unit` là tên đơn vị tiền đọc kèm ("đồng", "đô la Mỹ", …) — chứng từ ngoại
    tệ đọc theo nguyên tệ. Số nguyên thì thêm "chẵn" đúng thói quen viết tay
    trên phiếu; số có phần lẻ thì KHÔNG thêm (đọc "chẵn" cho một số lẻ là nói
    sai). Số âm đọc "Âm …" — phiếu thu/chi không bao giờ âm, nhưng hàm dùng
    chung nên không được im lặng nuốt dấu.
    """
    sign = "Âm " if value < _ZERO else ""
    magnitude = -value if value < _ZERO else value
    integral = int(magnitude)
    fraction_text = f"{magnitude - integral:f}"
    digits = fraction_text.split(".")[1].rstrip("0") if "." in fraction_text else ""
    words = _read_integer(integral)
    if digits:
        words = f"{words} phẩy {_read_fraction(digits)}"
    tail = f" {unit}" if unit else ""
    if not digits and unit:
        tail = f" {unit} chẵn"
    sentence = f"{sign}{words}{tail}"
    return sentence[0].upper() + sentence[1:]
