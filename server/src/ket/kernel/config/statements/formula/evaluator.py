"""Evaluator công thức chỉ tiêu: chạy theo thứ tự tô-pô của rowref (FR-GLE-043).

**Ngữ nghĩa bảy hàm.** Đầu vào là số liệu **một cột** của báo cáo — mỗi TK một
`AccountAmounts` gồm số dư tại thời điểm của cột (đã tách hai bên Ở MỨC TÀI
KHOẢN, cùng phép GREATEST với bảng cân đối TK — hai báo cáo phải khớp nhau,
BR-RPT-02) và phát sinh trong khoảng của cột:

* `DR(r)`  — tổng bên **dư Nợ** của các TK khớp dải (TK dư Có góp 0).
* `CR(r)`  — tổng bên **dư Có**.
* `BAL(r)` — dư ròng có dấu, Nợ dương (`DR - CR`).
* `DR_PS(r)` / `CR_PS(r)` — tổng **phát sinh thô** bên Nợ / bên Có trong khoảng.
* `DR_NET(r)` — phát sinh ròng bên Nợ có dấu (`DR_PS - CR_PS`); `CR_NET` ngược lại.

Điểm nhiều phần mềm sai (phase-05 §Layout): "dư Nợ chi tiết TK 131 theo từng
khách hàng" của thông tư cần sổ chi tiết đối tác — chưa có trước phase 7
(`ar_ap_ledger`, RT-22). Ở đây `DR(131)` là bên dư Nợ **sau khi gộp mọi đối
tượng**, tức phải-thu và người-mua-trả-trước bù trừ nhau ở mức TK. Đây là giới
hạn của *nguồn số* v1, không phải của công thức — phase 7 thay nguồn số bằng
số dư hai bên theo đối tượng thì công thức giữ nguyên.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from graphlib import CycleError, TopologicalSorter
from typing import Final

from ket.kernel.config.statements.formula.parser import (
    AccountFunctionCall,
    BinaryOperation,
    FormulaFunction,
    FormulaNode,
    NumberLiteral,
    RowReference,
    row_references_of,
)
from ket.kernel.errors import StatementFormulaInvalidError

ZERO: Final[Decimal] = Decimal(0)


@dataclass(frozen=True)
class AccountAmounts:
    """Số liệu một TK trong một cột báo cáo — số dư đã tách bên + phát sinh thô."""

    balance_debit: Decimal = ZERO
    balance_credit: Decimal = ZERO
    turnover_debit: Decimal = ZERO
    turnover_credit: Decimal = ZERO


def _function_value(call: AccountFunctionCall, amounts: dict[str, AccountAmounts]) -> Decimal:
    total = ZERO
    for code, account in amounts.items():
        if not any(account_range.matches(code) for account_range in call.ranges):
            continue
        match call.function:
            case FormulaFunction.DR:
                total += account.balance_debit
            case FormulaFunction.CR:
                total += account.balance_credit
            case FormulaFunction.BAL:
                total += account.balance_debit - account.balance_credit
            case FormulaFunction.DR_PS:
                total += account.turnover_debit
            case FormulaFunction.CR_PS:
                total += account.turnover_credit
            case FormulaFunction.DR_NET:
                total += account.turnover_debit - account.turnover_credit
            case FormulaFunction.CR_NET:
                total += account.turnover_credit - account.turnover_debit
    return total


def _node_value(
    node: FormulaNode,
    amounts: dict[str, AccountAmounts],
    resolved_rows: dict[str, Decimal],
) -> Decimal:
    match node:
        case NumberLiteral(value=value):
            return value
        case RowReference(row_code=code):
            # Toposort đã bảo đảm mọi rowref được tính trước — thiếu khóa ở đây
            # là lỗi lập trình của evaluator, không phải lỗi dữ liệu.
            return resolved_rows[code]
        case AccountFunctionCall() as call:
            return _function_value(call, amounts)
        case BinaryOperation(left=left, operator=operator, right=right):
            left_value = _node_value(left, amounts, resolved_rows)
            right_value = _node_value(right, amounts, resolved_rows)
            return left_value + right_value if operator == "+" else left_value - right_value


def evaluate_rows(
    formulas: dict[str, FormulaNode], amounts: dict[str, AccountAmounts]
) -> dict[str, Decimal]:
    """Giá trị mọi chỉ tiêu của một cột báo cáo.

    `formulas` là toàn bộ chỉ tiêu của một layout (mã chỉ tiêu → cây cú pháp).
    Rowref trỏ ra ngoài layout hoặc tạo chu trình là **lỗi cấu hình** — loader
    gói đã chặn từ lúc nhập, nhưng evaluator kiểm lại vì nó là ranh giới cuối
    trước khi một con số lên báo cáo tài chính.
    """
    dependencies = {code: row_references_of(node) for code, node in formulas.items()}
    for code, references in dependencies.items():
        unknown = references - formulas.keys()
        if unknown:
            raise StatementFormulaInvalidError(
                "Công thức tham chiếu chỉ tiêu không tồn tại trong layout",
                row_code=code,
                unknown=", ".join(sorted(unknown)),
            )
    sorter = TopologicalSorter(dependencies)
    try:
        ordered = tuple(sorter.static_order())
    except CycleError as error:
        cycle = error.args[1] if len(error.args) > 1 else ()
        raise StatementFormulaInvalidError(
            "Các chỉ tiêu tham chiếu vòng nhau — không có thứ tự tính hợp lệ",
            cycle=" -> ".join(str(code) for code in cycle),
        ) from error

    resolved: dict[str, Decimal] = {}
    for code in ordered:
        resolved[code] = _node_value(formulas[code], amounts, resolved)
    return resolved
