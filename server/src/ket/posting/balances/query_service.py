"""Bảng cân đối tài khoản — chọn giữa snapshot và tính-thẳng theo dấu bẩn.

Hợp đồng với người đọc số (phase-04 §Snapshot): kỳ **sạch** đọc
`account_balances` (nhanh); kỳ còn dấu bẩn tính thẳng từ `gl_postings` +
`opening_balances` (chậm hơn nhưng luôn đúng) và kết quả mang cờ `stale` để UI
hiện "đang chờ tính lại". Không bao giờ trả số cũ mà không nói.

Số ở đây GỘP theo tài khoản: cộng số ròng quy đổi của mọi (tiền tệ, chi nhánh)
rồi mới tách hai bên dư bằng GREATEST — bảng cân đối TK trình bày VND, mỗi TK
một bên dư; tách theo từng tiền tệ rồi cộng sẽ vẽ ra TK "vừa dư Nợ vừa dư Có"
chỉ vì nó cầm hai loại tiền. Test canh hai đường (snapshot / tính-thẳng) cho
cùng một con số. Dư hai bên theo **đối tượng** (TK lưỡng tính 131/331 đúng
nghĩa) cần sổ chi tiết đối tác — `ar_ap_ledger`, phase 7 (RT-22).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import Final

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.errors import PeriodNotFoundError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.posting.balances.models import AccountBalance, BalanceRecalcQueue

TRIAL_BALANCE_DIRECT_SQL: Final[str] = (
    resources.files("ket.posting.balances")
    .joinpath("sql/trial_balance_direct.sql")
    .read_text("utf-8")
)

ZERO: Final[Decimal] = Decimal(0)


@dataclass(frozen=True)
class TrialBalanceRow:
    """Một tài khoản trên bảng cân đối: dư đầu / phát sinh / dư cuối."""

    account_id: int
    account_code: str
    account_name: str
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class TrialBalanceResult:
    """Bảng cân đối + cờ nguồn số.

    `stale=True` nghĩa là số vừa tính thẳng từ sổ cái vì snapshot của kỳ chưa
    được tính lại — số **đúng**, chỉ chậm hơn; UI hiện chỉ báo "đang chờ tính
    lại" và gợi ý chạy job recalc.
    """

    rows: tuple[TrialBalanceRow, ...]
    stale: bool


def trial_balance(
    session: Session, *, ledger: int, period_id: int, branch_id: int | None = None
) -> TrialBalanceResult:
    """Bảng cân đối tài khoản của một (kỳ, sổ), gộp mọi chi nhánh nhìn thấy.

    `branch_id` thu hẹp thêm trong phạm vi RLS — RLS mới là hàng rào; tham số
    này chỉ là bộ lọc trình bày, đưa `branch_id` ngoài phạm vi chỉ cho kết quả
    rỗng chứ không cho xem trộm.
    """
    period = session.get(AccountingPeriod, period_id)
    if period is None:
        raise PeriodNotFoundError("Kỳ kế toán không tồn tại", period_id=period_id)
    year = session.get(FiscalYear, period.fiscal_year_id)
    if year is None:  # pragma: no cover - FK bảo đảm
        raise RuntimeError(f"Kỳ {period_id} trỏ vào năm tài chính không tồn tại")

    stale = _is_stale(session, ledger=ledger, period=period, branch_id=branch_id)
    if stale:
        amounts = _amounts_direct(
            session, ledger=ledger, period=period, year=year, branch_id=branch_id
        )
    else:
        amounts = _amounts_from_snapshot(
            session, ledger=ledger, period_id=period_id, branch_id=branch_id
        )
    return TrialBalanceResult(rows=_attach_accounts(session, amounts), stale=stale)


def _is_stale(
    session: Session, *, ledger: int, period: AccountingPeriod, branch_id: int | None
) -> bool:
    """Kỳ này còn dấu bẩn không — dấu ở kỳ sớm hơn CÙNG NĂM cũng làm bẩn kỳ này.

    Dư đầu kỳ lăn từ dư cuối kỳ trước nên bẩn lan xuôi trong năm; ranh giới năm
    chặn đường lan (xem docstring `recalc.py`). So theo `period_no` trong cùng
    `fiscal_year_id` chứ không so ngày giữa các năm.
    """
    dirty_from = (
        select(BalanceRecalcQueue.from_period_id)
        .join(AccountingPeriod, AccountingPeriod.id == BalanceRecalcQueue.from_period_id)
        .where(
            BalanceRecalcQueue.ledger == ledger,
            AccountingPeriod.fiscal_year_id == period.fiscal_year_id,
            AccountingPeriod.period_no <= period.period_no,
        )
    )
    if branch_id is not None:
        dirty_from = dirty_from.where(BalanceRecalcQueue.branch_id == branch_id)
    return bool(session.scalar(select(exists(dirty_from))))


AmountRow = tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]
"""`(account_id, dư đầu N/C, phát sinh N/C, dư cuối N/C)` — trước khi gắn tên TK."""


def _amounts_from_snapshot(
    session: Session, *, ledger: int, period_id: int, branch_id: int | None
) -> list[AmountRow]:
    # Hai bên dư tách Ở MỨC TÀI KHOẢN sau khi cộng mọi (tiền tệ, chi nhánh):
    # dòng snapshot giữ hai bên theo từng tiền tệ, và cộng thẳng hai cột đó sẽ
    # cho một TK "vừa dư Nợ vừa dư Có" chỉ vì nó cầm hai loại tiền. Cùng phép
    # toán với câu cuối của `trial_balance_direct.sql` — hai đường phải khớp.
    opening_net = func.sum(AccountBalance.opening_debit - AccountBalance.opening_credit)
    closing_net = func.sum(AccountBalance.closing_debit - AccountBalance.closing_credit)
    statement = (
        select(
            AccountBalance.account_id,
            func.greatest(opening_net, 0),
            func.greatest(-opening_net, 0),
            func.sum(AccountBalance.period_debit),
            func.sum(AccountBalance.period_credit),
            func.greatest(closing_net, 0),
            func.greatest(-closing_net, 0),
        )
        .where(AccountBalance.period_id == period_id, AccountBalance.ledger == ledger)
        .group_by(AccountBalance.account_id)
        .having(
            ~(
                (opening_net == 0)
                & (func.sum(AccountBalance.period_debit) == 0)
                & (func.sum(AccountBalance.period_credit) == 0)
            )
        )
    )
    if branch_id is not None:
        statement = statement.where(AccountBalance.branch_id == branch_id)
    return [
        (row[0], row[1], row[2], row[3], row[4], row[5], row[6])
        for row in session.execute(statement).all()
    ]


def _amounts_direct(
    session: Session,
    *,
    ledger: int,
    period: AccountingPeriod,
    year: FiscalYear,
    branch_id: int | None,
) -> list[AmountRow]:
    rows = session.execute(
        text(TRIAL_BALANCE_DIRECT_SQL),
        {
            "ledger": ledger,
            "fiscal_year_id": year.id,
            "year_start": year.start_date,
            "period_start": period.start_date,
            "period_end": period.end_date,
            "branch_id": branch_id,
        },
    ).all()
    return [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows]


def _attach_accounts(session: Session, amounts: list[AmountRow]) -> tuple[TrialBalanceRow, ...]:
    """Gắn số hiệu + tên TK và xếp theo số hiệu — thứ tự người đọc sổ mong."""
    if not amounts:
        return ()
    account_ids = [row[0] for row in amounts]
    accounts = {
        account.id: account
        for account in session.execute(
            select(ChartOfAccount).where(ChartOfAccount.id.in_(account_ids))
        ).scalars()
    }
    rows = []
    for account_id, ob_d, ob_c, p_d, p_c, cl_d, cl_c in amounts:
        account = accounts.get(account_id)
        if account is None:  # pragma: no cover - FK bảo đảm
            raise RuntimeError(f"Dòng số dư trỏ vào tài khoản không tồn tại: {account_id}")
        rows.append(
            TrialBalanceRow(
                account_id=account_id,
                account_code=account.code,
                account_name=account.name,
                opening_debit=ob_d or ZERO,
                opening_credit=ob_c or ZERO,
                period_debit=p_d or ZERO,
                period_credit=p_c or ZERO,
                closing_debit=cl_d or ZERO,
                closing_credit=cl_c or ZERO,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.account_code))
