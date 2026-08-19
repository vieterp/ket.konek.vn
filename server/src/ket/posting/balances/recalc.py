"""Tính lại snapshot số dư từ hàng đợi dấu bẩn — nửa "đọc" của ADR-011.

Chạy **theo chi nhánh** (quyết định bàn giao 4A→4B): job kế thừa phạm vi RLS
của người xếp hàng, tức là một chi nhánh, và mọi bảng nó chạm
(`balance_recalc_queue`, `gl_postings`, `account_balances`, `opening_balances`)
đều bị RLS lọc theo đúng chi nhánh đó. Không cần kết nối đặc quyền; chi nhánh
khác bẩn thì chi nhánh đó tự chạy (hoặc lock service của 4D — nơi duy nhất phải
nhìn mọi chi nhánh — tự lo phần kiểm của nó).

Mỗi kỳ là một cặp câu **set-based** DELETE + INSERT (LD-14, ADR-014):

* DELETE quét sạch lát snapshot của kỳ trước khi tính — dòng mồ côi (khóa chỉ
  tồn tại nhờ chứng từ đã bị bỏ ghi sổ) chết ở đây; `ON CONFLICT DO UPDATE`
  như phase file gợi ý **không** giết được chúng nên đổi sang cặp này, lệch có
  chủ đích đã ghi vào báo cáo slice.
* INSERT (`sql/recalc_period.sql`) tính toàn bộ dòng của kỳ từ `gl_postings` +
  dư cuối kỳ trước (hoặc `opening_balances` cho kỳ đầu năm). Python chỉ điều
  phối thứ tự kỳ — không sờ vào dòng dữ liệu nào.

Dấu bẩn lan **trong một năm tài chính**: dư đầu kỳ lăn từ dư cuối kỳ trước,
nên bẩn ở kỳ N kéo theo mọi kỳ sau nó *cùng năm*. Sang năm mới thì dư đầu lấy
từ `opening_balances` — bảng đó do job chuyển số dư (4C) ghi tường minh, không
lăn ngầm, nên ranh giới năm chặn đường lan (FR-OPB-010: chuyển năm là thao tác
có xác nhận, không phải hệ quả phụ của tính lại).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Select, delete, select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import PeriodNotFoundError
from ket.kernel.periods.models import AccountingPeriod
from ket.posting.balances.models import AccountBalance, BalanceRecalcQueue

RECALC_PERIOD_SQL: Final[str] = (
    resources.files("ket.posting.balances").joinpath("sql/recalc_period.sql").read_text("utf-8")
)


@dataclass(frozen=True)
class DirtyMark:
    """Một dòng hàng đợi như job đã đọc nó — `marked_at` là "phiên bản" của dấu.

    Xóa dấu phải so đủ bộ ba khóa **và** `marked_at`: một lượt ghi sổ chen vào
    giữa lúc job đang tính sẽ làm mới `marked_at` (xem `mark_dirty`), và phép
    so-bằng khiến dấu vừa làm mới sống sót — kỳ đó được tính lại ở lượt sau
    thay vì bị coi là sạch oan.
    """

    ledger: int
    from_period_id: int
    marked_at: datetime


@dataclass(frozen=True)
class RecalcRun:
    """Một dải kỳ liên tiếp phải tính, trong MỘT năm tài chính, cho một sổ."""

    ledger: int
    periods: tuple[AccountingPeriod, ...]


@dataclass(frozen=True)
class RecalcOutcome:
    """Kết quả một lượt tính lại — con số cho `jobs.result` và cho test."""

    periods_recalced: int
    marks_cleared: int


def pending_marks(session: Session, *, branch_id: int) -> tuple[DirtyMark, ...]:
    """Dấu bẩn của chi nhánh, kèm `marked_at` làm phiên bản để xóa an toàn."""
    rows = session.execute(
        select(
            BalanceRecalcQueue.ledger,
            BalanceRecalcQueue.from_period_id,
            BalanceRecalcQueue.marked_at,
        )
        .where(BalanceRecalcQueue.branch_id == branch_id)
        .order_by(BalanceRecalcQueue.ledger, BalanceRecalcQueue.from_period_id)
    ).all()
    return tuple(
        DirtyMark(ledger=row.ledger, from_period_id=row.from_period_id, marked_at=row.marked_at)
        for row in rows
    )


def _periods_from(session: Session, from_period_id: int) -> tuple[AccountingPeriod, ...]:
    """Kỳ bắt đầu + mọi kỳ sau nó trong CÙNG năm tài chính, theo thứ tự."""
    start = session.get(AccountingPeriod, from_period_id)
    if start is None:
        raise PeriodNotFoundError("Kỳ kế toán cần tính lại không tồn tại", period_id=from_period_id)
    statement: Select[tuple[AccountingPeriod]] = (
        select(AccountingPeriod)
        .where(
            AccountingPeriod.fiscal_year_id == start.fiscal_year_id,
            AccountingPeriod.period_no >= start.period_no,
        )
        .order_by(AccountingPeriod.period_no)
    )
    return tuple(session.execute(statement).scalars().all())


def build_runs(
    session: Session,
    marks: tuple[DirtyMark, ...],
    *,
    forced: tuple[tuple[int, int], ...] = (),
) -> tuple[RecalcRun, ...]:
    """Gộp dấu bẩn (+ yêu cầu ép `(sổ, kỳ)`) thành các dải kỳ phải tính.

    Nhiều dấu của cùng `(sổ, năm)` gộp về dấu **sớm nhất** — dải kỳ từ đó đã
    phủ mọi dấu muộn hơn. Dấu ở hai năm khác nhau là hai dải riêng: ranh giới
    năm chặn đường lan (xem docstring module).
    """
    starts: dict[tuple[int, int], AccountingPeriod] = {}

    def offer(ledger: int, period: AccountingPeriod) -> None:
        key = (ledger, period.fiscal_year_id)
        current = starts.get(key)
        if current is None or period.period_no < current.period_no:
            starts[key] = period

    for mark in marks:
        period = session.get(AccountingPeriod, mark.from_period_id)
        if period is None:  # pragma: no cover - FK trên queue bảo đảm
            raise PeriodNotFoundError(
                "Dấu bẩn trỏ vào kỳ không tồn tại", period_id=mark.from_period_id
            )
        offer(mark.ledger, period)
    for ledger, period_id in forced:
        period = session.get(AccountingPeriod, period_id)
        if period is None:
            raise PeriodNotFoundError("Kỳ kế toán cần tính lại không tồn tại", period_id=period_id)
        offer(ledger, period)

    return tuple(
        RecalcRun(ledger=ledger, periods=_periods_from(session, start.id))
        for (ledger, _year), start in sorted(
            starts.items(), key=lambda item: (item[0][0], item[1].start_date)
        )
    )


def recalc_period(
    session: Session, *, ledger: int, branch_id: int, period: AccountingPeriod
) -> None:
    """Tính lại lát snapshot của một (kỳ, sổ, chi nhánh) — DELETE rồi INSERT."""
    previous_id = session.scalar(
        select(AccountingPeriod.id).where(
            AccountingPeriod.fiscal_year_id == period.fiscal_year_id,
            AccountingPeriod.period_no == period.period_no - 1,
        )
    )
    session.execute(
        delete(AccountBalance).where(
            AccountBalance.period_id == period.id,
            AccountBalance.ledger == ledger,
            AccountBalance.branch_id == branch_id,
        )
    )
    session.execute(
        text(RECALC_PERIOD_SQL),
        {
            "ledger": ledger,
            "branch_id": branch_id,
            "period_id": period.id,
            "prev_period_id": previous_id,
            "fiscal_year_id": period.fiscal_year_id,
        },
    )


def clear_marks(session: Session, *, branch_id: int, marks: tuple[DirtyMark, ...]) -> int:
    """Xóa đúng những phiên bản dấu bẩn đã đọc; dấu được làm mới thì để lại."""
    cleared = 0
    for mark in marks:
        # `Session.execute` khai kiểu trả về là `Result`; DML thì luôn là
        # `CursorResult`, nơi `rowcount` có thật (cùng lý do ở `auth_service`).
        result = cast(
            "CursorResult[Any]",
            session.execute(
                delete(BalanceRecalcQueue).where(
                    BalanceRecalcQueue.ledger == mark.ledger,
                    BalanceRecalcQueue.branch_id == branch_id,
                    BalanceRecalcQueue.from_period_id == mark.from_period_id,
                    BalanceRecalcQueue.marked_at == mark.marked_at,
                )
            ),
        )
        cleared += result.rowcount
    return cleared
