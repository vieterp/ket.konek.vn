"""Nguồn số của statement builder: một năm tài chính → số liệu từng tài khoản.

Trả về hai "cột nhìn" (`ColumnAmounts`) dựng được từ MỘT lượt quét sổ:

* **instant cuối khoảng** — dư tại `range_end` (dư đầu năm + phát sinh lũy kế),
  tách hai bên ở mức tài khoản (GREATEST, cùng phép với bảng cân đối TK);
* **instant đầu năm** — dư `opening_balances`, phát sinh để 0 (cột "Số đầu năm"
  của báo cáo tình hình tài chính không có khái niệm phát sinh).

Số dư "chi tiết theo đối tượng" của thông tư (131/331 hai bên theo từng khách
hàng/người bán) cần `ar_ap_ledger` — phase 7 thay hàm dựng `ColumnAmounts`
bằng nguồn hai bên theo đối tượng, công thức và evaluator giữ nguyên (xem
docstring `kernel/config/statements/formula/evaluator.py`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib import resources
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.kernel.config.statements.formula.evaluator import AccountAmounts

STATEMENT_BALANCES_SQL: Final[str] = (
    resources.files("ket.reporting.statements")
    .joinpath("sql/statement_balances.sql")
    .read_text("utf-8")
)

ZERO: Final[Decimal] = Decimal(0)


ColumnAmounts = dict[str, AccountAmounts]
"""Mã TK → số liệu của MỘT cột báo cáo — đầu vào của `evaluate_rows`."""


def _split(net: Decimal) -> tuple[Decimal, Decimal]:
    """Tách dư ròng thành (bên Nợ, bên Có) ở mức tài khoản — GREATEST."""
    return (net, ZERO) if net >= 0 else (ZERO, -net)


def load_fiscal_year_amounts(
    session: Session,
    *,
    ledger: int,
    fiscal_year_id: int,
    year_start: date,
    range_end: date,
    branch_id: int | None,
    closing_in_turnover: bool,
) -> tuple[ColumnAmounts, ColumnAmounts]:
    """`(cột instant cuối khoảng, cột instant đầu năm)` của một năm tài chính.

    Cột thứ nhất mang cả phát sinh lũy kế `year_start..range_end` — các layout
    `income`/`cashflow` đọc phát sinh từ đúng cột này (một năm = một lượt quét,
    cột "Năm trước" là một lượt gọi thứ hai trên năm trước).

    `closing_in_turnover=False` bỏ bút toán kết chuyển khỏi **phát sinh** (LD-17)
    — chỉ đúng cho layout đo phát sinh nghiệp vụ (B02). Số dư thì **luôn** gồm
    kết chuyển, nên gọi với `False` xong đọc `balance_*` sẽ ra số vô nghĩa: bảng
    cân đối phải gọi `True`. Bất biến này do `builder` giữ (nó chọn theo
    `statement_kind`), và test `test_balance_sheet_includes_closing_entries`
    canh cho nó không trôi.
    """
    rows = session.execute(
        text(STATEMENT_BALANCES_SQL),
        {
            "ledger": ledger,
            "fiscal_year_id": fiscal_year_id,
            "year_start": year_start,
            "range_end": range_end,
            "branch_id": branch_id,
            "closing_in_turnover": closing_in_turnover,
        },
    ).all()

    closing_column: ColumnAmounts = {}
    opening_column: ColumnAmounts = {}
    for account_code, opening_net, turnover_debit, turnover_credit in rows:
        opening = opening_net or ZERO
        debit = turnover_debit or ZERO
        credit = turnover_credit or ZERO
        closing_debit, closing_credit = _split(opening + debit - credit)
        closing_column[account_code] = AccountAmounts(
            balance_debit=closing_debit,
            balance_credit=closing_credit,
            turnover_debit=debit,
            turnover_credit=credit,
        )
        opening_debit, opening_credit = _split(opening)
        opening_column[account_code] = AccountAmounts(
            balance_debit=opening_debit,
            balance_credit=opening_credit,
        )
    return closing_column, opening_column
