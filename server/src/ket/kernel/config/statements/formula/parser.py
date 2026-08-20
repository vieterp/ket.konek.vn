"""Lexer + parser đệ quy xuống cho grammar công thức BCTC — không `eval`.

Grammar khai ở `formula/__init__.py`. Không có ngoặc nhóm và không có nhân/chia:
mẫu thông tư chỉ cộng trừ chỉ tiêu và hàm số dư — `{30 = 20 + 21 + 22 - (23 +
25 + 26)}` của B02 viết phẳng thành `[20] + [21] + [22] - [23] - [25] - [26]`.
Thứ gì grammar không nói được là tín hiệu chỉ tiêu đó cần dataset riêng (LCTT —
hoãn 10a), không phải lý do mở rộng grammar thành ngôn ngữ biểu thức.

Mọi lỗi ném `StatementFormulaInvalidError` kèm vị trí — loader gói cấu hình
bọc thêm ngữ cảnh (layout nào, chỉ tiêu nào) rồi từ chối cả gói (fail-closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from ket.kernel.config.statements.formula.account_range import AccountRange, make_range
from ket.kernel.errors import StatementFormulaInvalidError


class FormulaFunction(Enum):
    """Bảy hàm số liệu — ngữ nghĩa chốt ở `evaluator.py`."""

    DR = auto()
    CR = auto()
    BAL = auto()
    DR_PS = auto()
    CR_PS = auto()
    DR_NET = auto()
    CR_NET = auto()


_FUNCTIONS_BY_NAME = {member.name: member for member in FormulaFunction}


# --- Cây cú pháp ------------------------------------------------------------


@dataclass(frozen=True)
class NumberLiteral:
    value: Decimal


@dataclass(frozen=True)
class RowReference:
    row_code: str


@dataclass(frozen=True)
class AccountFunctionCall:
    function: FormulaFunction
    ranges: tuple[AccountRange, ...]


@dataclass(frozen=True)
class BinaryOperation:
    left: FormulaNode
    operator: str
    """`'+'` hoặc `'-'` — grammar chỉ có hai."""
    right: FormulaNode


FormulaNode = NumberLiteral | RowReference | AccountFunctionCall | BinaryOperation


def row_references_of(node: FormulaNode) -> frozenset[str]:
    """Mọi rowref trong một công thức — đầu vào dựng DAG chỉ tiêu của evaluator."""
    match node:
        case RowReference(row_code=code):
            return frozenset({code})
        case BinaryOperation(left=left, right=right):
            return row_references_of(left) | row_references_of(right)
        case _:
            return frozenset()


def account_ranges_of(node: FormulaNode) -> tuple[AccountRange, ...]:
    """Mọi dải TK trong một công thức — loader kiểm chúng khớp `accounts.csv`."""
    match node:
        case AccountFunctionCall(ranges=ranges):
            return ranges
        case BinaryOperation(left=left, right=right):
            return account_ranges_of(left) + account_ranges_of(right)
        case _:
            return ()


# --- Lexer ------------------------------------------------------------------


class _TokenKind(Enum):
    NUMBER = auto()
    IDENT = auto()
    ROWREF = auto()
    PLUS = auto()
    MINUS = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    STAR = auto()
    DOTDOT = auto()
    END = auto()


@dataclass(frozen=True)
class _Token:
    kind: _TokenKind
    text: str
    position: int


def _fail(formula: str, position: int, reason: str) -> StatementFormulaInvalidError:
    return StatementFormulaInvalidError(reason, formula=formula, position=position)


def _is_ascii_digit(char: str) -> bool:
    return "0" <= char <= "9"


def _is_ascii_alpha(char: str) -> bool:
    return "a" <= char <= "z" or "A" <= char <= "Z"


_ROWREF_PATTERN = re.compile(r"^[A-Za-z0-9]+$")


def _tokenize(formula: str) -> list[_Token]:
    # Chữ số/chữ cái kiểm ASCII tường minh, không `str.isdigit()/isalnum()`:
    # hai hàm đó nhận cả chữ số Unicode ("١٢٣") — một mã TK "trông như số"
    # nhưng không bao giờ khớp accounts.csv (review 5B, L-2).
    tokens: list[_Token] = []
    index = 0
    length = len(formula)
    while index < length:
        char = formula[index]
        if char.isspace():
            index += 1
            continue
        if _is_ascii_digit(char):
            start = index
            while index < length and _is_ascii_digit(formula[index]):
                index += 1
            tokens.append(_Token(_TokenKind.NUMBER, formula[start:index], start))
            continue
        if _is_ascii_alpha(char) or char == "_":
            start = index
            while index < length and (
                _is_ascii_alpha(formula[index])
                or _is_ascii_digit(formula[index])
                or formula[index] == "_"
            ):
                index += 1
            tokens.append(_Token(_TokenKind.IDENT, formula[start:index], start))
            continue
        if char == "[":
            end = formula.find("]", index + 1)
            if end < 0:
                raise _fail(formula, index, "Thiếu dấu `]` đóng tham chiếu chỉ tiêu")
            inner = formula[index + 1 : end].strip()
            if not inner or not _ROWREF_PATTERN.fullmatch(inner):
                raise _fail(formula, index, "Mã chỉ tiêu trong `[...]` phải là chữ/số, không rỗng")
            tokens.append(_Token(_TokenKind.ROWREF, inner, index))
            index = end + 1
            continue
        if char == ".":
            if formula[index : index + 2] == "..":
                tokens.append(_Token(_TokenKind.DOTDOT, "..", index))
                index += 2
                continue
            raise _fail(formula, index, "Dấu `.` lẻ — dải tài khoản viết dạng `131..138`")
        simple = {
            "+": _TokenKind.PLUS,
            "-": _TokenKind.MINUS,
            "(": _TokenKind.LPAREN,
            ")": _TokenKind.RPAREN,
            ",": _TokenKind.COMMA,
            "*": _TokenKind.STAR,
        }.get(char)
        if simple is None:
            raise _fail(formula, index, f"Ký tự không thuộc grammar công thức: `{char}`")
        tokens.append(_Token(simple, char, index))
        index += 1
    tokens.append(_Token(_TokenKind.END, "", length))
    return tokens


# --- Parser -----------------------------------------------------------------


class _Parser:
    def __init__(self, formula: str) -> None:
        self._formula = formula
        self._tokens = _tokenize(formula)
        self._index = 0

    def parse(self) -> FormulaNode:
        node = self._expression()
        trailing = self._peek()
        if trailing.kind is not _TokenKind.END:
            raise _fail(
                self._formula, trailing.position, f"Thừa nội dung sau công thức: `{trailing.text}`"
            )
        return node

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _expect(self, kind: _TokenKind, description: str) -> _Token:
        token = self._peek()
        if token.kind is not kind:
            raise _fail(self._formula, token.position, f"Chờ {description}")
        return self._advance()

    def _expression(self) -> FormulaNode:
        node = self._term()
        while self._peek().kind in (_TokenKind.PLUS, _TokenKind.MINUS):
            operator = self._advance()
            right = self._term()
            node = BinaryOperation(left=node, operator=operator.text, right=right)
        return node

    def _term(self) -> FormulaNode:
        token = self._peek()
        if token.kind is _TokenKind.NUMBER:
            self._advance()
            return NumberLiteral(Decimal(token.text))
        if token.kind is _TokenKind.ROWREF:
            self._advance()
            return RowReference(token.text)
        if token.kind is _TokenKind.IDENT:
            return self._account_function()
        raise _fail(
            self._formula,
            token.position,
            "Chờ một số, `[mã chỉ tiêu]` hoặc hàm số dư (DR/CR/BAL/DR_PS/CR_PS/DR_NET/CR_NET)",
        )

    def _account_function(self) -> AccountFunctionCall:
        name = self._advance()
        function = _FUNCTIONS_BY_NAME.get(name.text)
        if function is None:
            raise _fail(
                self._formula,
                name.position,
                f"Hàm `{name.text}` không thuộc grammar "
                "(DR, CR, BAL, DR_PS, CR_PS, DR_NET, CR_NET)",
            )
        self._expect(_TokenKind.LPAREN, "dấu `(` sau tên hàm")
        ranges = [self._account_range()]
        while self._peek().kind is _TokenKind.COMMA:
            self._advance()
            ranges.append(self._account_range())
        self._expect(_TokenKind.RPAREN, "dấu `)` đóng danh sách dải tài khoản")
        return AccountFunctionCall(function=function, ranges=tuple(ranges))

    def _account_range(self) -> AccountRange:
        low = self._expect(_TokenKind.NUMBER, "một số hiệu tài khoản")
        token = self._peek()
        if token.kind is _TokenKind.STAR:
            self._advance()
            # `11*` và `11` cùng nghĩa (khớp tiền tố) — dấu `*` chỉ là cách
            # người soạn nói rõ "cả nhánh".
            return make_range(low.text, None)
        if token.kind is _TokenKind.DOTDOT:
            self._advance()
            high = self._expect(_TokenKind.NUMBER, "cận trên của dải tài khoản sau `..`")
            return make_range(low.text, high.text)
        return make_range(low.text, None)


def parse_formula(formula: str) -> FormulaNode:
    """Parse một công thức chỉ tiêu; ném `StatementFormulaInvalidError` kèm vị trí."""
    stripped = formula.strip()
    if not stripped:
        raise StatementFormulaInvalidError("Công thức rỗng", formula=formula)
    return _Parser(stripped).parse()
