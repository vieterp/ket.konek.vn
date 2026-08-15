"""Danh mục tùy chọn: khóa nào tồn tại, kiểu gì, mặc định bao nhiêu (FR-SYS-060).

Catalog **đóng** — chỉ khóa khai ở đây mới ghi được. Một bảng key-value mở là
nơi mọi thứ chưa kịp thiết kế sẽ rơi vào: ba phase nữa sẽ có vài chục khóa mà
không ai biết khóa nào còn được đọc, khóa nào là rác của một lần thử nghiệm, và
đổi kiểu của một khóa sẽ là canh bạc. Khai ở đây thì màn hình thiết lập dựng
được từ chính danh mục này, và `mypy` canh được nơi đọc.

Ràng buộc giá trị (`choices`, `minimum`, `maximum`) nằm cùng chỗ với khóa chứ
không nằm trong mã đọc nó: `money.scale = 9` phải bị từ chối **lúc ghi**, ở màn
hình thiết lập, chứ không phải nổ ra giữa một lần tính giá thành ba tháng sau.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from ket.kernel.errors import SettingValueInvalidError
from ket.kernel.money import MONEY_SCALE_MAX

SettingValue = str | int | bool | Decimal
"""Giá trị vô hướng của một tùy chọn. Không có cấu trúc lồng nhau — xem
docstring của `Setting` về lý do lưu chuỗi có kiểu thay vì JSONB."""


class SettingScope(StrEnum):
    """Cấp của một tùy chọn (BR-SYS-06)."""

    SYSTEM = "system"
    USER = "user"


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DECIMAL = "decimal"


TRUE_LITERAL: Final[str] = "true"
FALSE_LITERAL: Final[str] = "false"


@dataclass(frozen=True)
class SettingDefinition:
    """Một khóa tùy chọn và mọi thứ cần biết để hiển thị, kiểm và đọc nó."""

    key: str
    value_type: ValueType
    default: str
    """Mặc định lưu ở dạng **chuỗi**, cùng dạng với cột `settings.value`: một
    đường phân giải duy nhất cho cả giá trị mặc định lẫn giá trị đã lưu, nên
    không có nhánh nào chỉ chạy khi người dùng đã từng ghi."""

    scopes: frozenset[SettingScope]
    description: str
    choices: frozenset[str] | None = None
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        # Mặc định phải tự đi qua chính bộ kiểm của nó — nếu không, một khóa
        # khai sai sẽ chỉ lộ ra khi có người đầu tiên đọc nó.
        parse_value(self, self.default)


def parse_value(definition: SettingDefinition, raw: str) -> SettingValue:
    """Chuỗi trong DB → giá trị có kiểu. Sai kiểu/ngoài ràng buộc → `422`."""
    match definition.value_type:
        case ValueType.STRING:
            if definition.choices is not None and raw not in definition.choices:
                raise SettingValueInvalidError(
                    "Giá trị không nằm trong danh sách cho phép",
                    key=definition.key,
                    allowed=", ".join(sorted(definition.choices)),
                )
            return raw
        case ValueType.INTEGER:
            try:
                number = int(raw)
            except ValueError:
                raise SettingValueInvalidError(
                    "Giá trị phải là số nguyên", key=definition.key, value=raw
                ) from None
            _check_bounds(definition, number)
            return number
        case ValueType.BOOLEAN:
            if raw not in (TRUE_LITERAL, FALSE_LITERAL):
                raise SettingValueInvalidError(
                    f"Giá trị phải là `{TRUE_LITERAL}` hoặc `{FALSE_LITERAL}`",
                    key=definition.key,
                    value=raw,
                )
            return raw == TRUE_LITERAL
        case ValueType.DECIMAL:
            try:
                # `Decimal(str)` chứ không bao giờ qua `float`: mọi con số của
                # hệ này là tiền hoặc tỷ lệ dùng để tính tiền (LD-03).
                return Decimal(raw)
            except InvalidOperation:
                raise SettingValueInvalidError(
                    "Giá trị phải là số thập phân", key=definition.key, value=raw
                ) from None


def _check_bounds(definition: SettingDefinition, number: int) -> None:
    if definition.minimum is not None and number < definition.minimum:
        raise SettingValueInvalidError(
            "Giá trị nhỏ hơn mức tối thiểu", key=definition.key, minimum=definition.minimum
        )
    if definition.maximum is not None and number > definition.maximum:
        raise SettingValueInvalidError(
            "Giá trị lớn hơn mức tối đa", key=definition.key, maximum=definition.maximum
        )


MONEY_SCALE_KEY: Final[str] = "money.scale"
LOCALE_KEY: Final[str] = "ui.locale"
GRID_ENTER_KEY: Final[str] = "ui.grid_enter_moves_to_next_row"

CATALOG: Final[dict[str, SettingDefinition]] = {
    definition.key: definition
    for definition in (
        SettingDefinition(
            key=MONEY_SCALE_KEY,
            value_type=ValueType.INTEGER,
            default="2",
            # Chỉ cấp hệ thống: số chữ số thập phân quyết định cách **làm tròn
            # khi ghi sổ** (FR-NFR-002, FR-SYS-064). Cho phép mỗi người một giá
            # trị nghĩa là hai kế toán viên nhập cùng một hóa đơn ra hai con số
            # khác nhau — đúng thứ mục tiêu chất lượng số một cấm.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Số chữ số thập phân khi làm tròn tiền",
            minimum=0,
            maximum=MONEY_SCALE_MAX,
        ),
        SettingDefinition(
            key=LOCALE_KEY,
            value_type=ValueType.STRING,
            default="vi",
            scopes=frozenset({SettingScope.SYSTEM, SettingScope.USER}),
            description="Ngôn ngữ giao diện (FR-NFR-034)",
            choices=frozenset({"vi", "en"}),
        ),
        SettingDefinition(
            key=GRID_ENTER_KEY,
            value_type=ValueType.BOOLEAN,
            default=TRUE_LITERAL,
            # Thói quen bàn phím là thứ **thuộc về từng người** (FR-NFR-052):
            # người quen Excel muốn Enter xuống dòng, người quen phần mềm cũ
            # muốn Enter sang ô kế. Ép chung một giá trị là cách chắc chắn làm
            # chậm một nửa số người nhập liệu.
            scopes=frozenset({SettingScope.SYSTEM, SettingScope.USER}),
            description="Phím Enter chuyển xuống dòng kế trong lưới nhập liệu",
        ),
    )
}


def definition_of(key: str) -> SettingDefinition | None:
    return CATALOG.get(key)
