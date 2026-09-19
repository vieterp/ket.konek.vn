"""Xem trước FR-STK-003 — chứng từ bị ảnh hưởng khi tính lại giá xuất kho, và
kỳ đã khóa bị chạm (RT-11, phương án A).

Cùng CTE `keys` với các câu phương pháp: khóa cần tính + ngày bắt đầu của khóa.
Hai câu gộp ở SQL (`affected_periods.sql` theo kỳ + dòng tổng,
`affected_vouchers.sql` theo chứng từ có `LIMIT`) — Python chỉ đổ kết quả vào
schema, không đếm trên từng movement (ADR-014; review 8B M-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.periods.models import FiscalYear
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.inventory.models import CostState, InventoryMovement, InventoryRecalcMark
from ket.modules.inventory.schemas import (
    AffectedVoucher,
    CostingAffectedPreview,
    LockedPeriodTouched,
)

AFFECTED_VOUCHER_LIMIT: Final[int] = 500
"""Xem trước liệt kê tối đa chừng này chứng từ — đủ để đọc, không phải để tải
cả năm về client; `voucher_count` mang tổng thật."""

_SQL_ROOT: Final = resources.files("ket.modules.inventory.costing").joinpath("sql")
AFFECTED_PERIODS_SQL: Final[str] = _SQL_ROOT.joinpath("affected_periods.sql").read_text("utf-8")
AFFECTED_VOUCHERS_SQL: Final[str] = _SQL_ROOT.joinpath("affected_vouchers.sql").read_text("utf-8")


@dataclass(frozen=True)
class YearSummary:
    """Dòng tổng + các kỳ khóa bị chạm của một năm trong horizon."""

    keys: int
    movements: int
    vouchers: int
    earliest: date | None
    locked: tuple[LockedPeriodTouched, ...]


def years_to_process(
    session: Session, *, branch_id: int, force_from: date | None
) -> tuple[FiscalYear, ...]:
    """Các năm tài chính có việc để tính, theo thứ tự thời gian.

    Ép từ một ngày → chỉ năm chứa ngày ấy. Còn lại: mọi năm phủ ít nhất một
    dấu bẩn hoặc một movement chưa tính giá của chi nhánh. Horizon ghi cắt ở
    cuối mỗi năm (cùng luật `lock_check`), nên mỗi năm là một lượt riêng.
    """
    if force_from is not None:
        year = fiscal_year_covering(session, force_from)
        return (year,) if year is not None else ()
    mark_dates = session.execute(
        select(InventoryRecalcMark.from_date).where(InventoryRecalcMark.branch_id == branch_id)
    ).scalars()
    uncosted_dates = session.execute(
        select(InventoryMovement.posting_date)
        .where(
            InventoryMovement.branch_id == branch_id,
            InventoryMovement.cost_state != CostState.COSTED,
            InventoryMovement.is_custodial.is_(False),
        )
        .distinct()
    ).scalars()
    dates = sorted({*mark_dates, *uncosted_dates})
    if not dates:
        return ()
    years = session.execute(
        select(FiscalYear)
        .where(FiscalYear.end_date >= dates[0], FiscalYear.start_date <= dates[-1])
        .order_by(FiscalYear.start_date)
    ).scalars()
    return tuple(
        year for year in years if any(year.start_date <= d <= year.end_date for d in dates)
    )


def year_summary(
    session: Session, *, branch_id: int, year: FiscalYear, force_from: date | None
) -> YearSummary:
    rows = session.execute(text(AFFECTED_PERIODS_SQL), _params(branch_id, year, force_from)).all()
    total = next((row for row in rows if row.period_id is None), None)
    locked = tuple(
        LockedPeriodTouched(
            period_id=row.period_id, period_no=row.period_no, movements=int(row.movements)
        )
        for row in rows
        if row.period_id is not None and row.period_locked
    )
    if total is None:
        return YearSummary(keys=0, movements=0, vouchers=0, earliest=None, locked=locked)
    return YearSummary(
        keys=int(total.keys),
        movements=int(total.movements),
        vouchers=int(total.vouchers),
        earliest=total.earliest,
        locked=locked,
    )


def locked_periods_touched(
    session: Session, *, branch_id: int, year: FiscalYear, force_from: date | None
) -> tuple[LockedPeriodTouched, ...]:
    """Kỳ đã khóa mà horizon của năm này sẽ ghi vào — rỗng là điều kiện chạy."""
    return year_summary(session, branch_id=branch_id, year=year, force_from=force_from).locked


def preview(session: Session, *, branch_id: int, force_from: date | None) -> CostingAffectedPreview:
    """Gộp mọi năm có việc thành một bản xem trước cho màn "Tính giá xuất kho"."""
    years = years_to_process(session, branch_id=branch_id, force_from=force_from)
    summaries = [
        year_summary(session, branch_id=branch_id, year=year, force_from=force_from)
        for year in years
    ]
    vouchers: list[AffectedVoucher] = []
    for year in years:
        if len(vouchers) >= AFFECTED_VOUCHER_LIMIT:
            break
        rows = session.execute(
            text(AFFECTED_VOUCHERS_SQL),
            {
                **_params(branch_id, year, force_from),
                "limit": AFFECTED_VOUCHER_LIMIT - len(vouchers),
            },
        ).all()
        vouchers.extend(
            AffectedVoucher(
                voucher_id=row.voucher_id,
                voucher_no=row.voucher_no,
                document_type=row.document_type,
                posting_date=row.posting_date,
                movements=int(row.movements),
            )
            for row in rows
        )
    earliest = min((s.earliest for s in summaries if s.earliest is not None), default=None)
    movements = sum(s.movements for s in summaries)
    return CostingAffectedPreview(
        branch_id=branch_id,
        valuation_method=years[0].inventory_valuation_method if years and movements else None,
        earliest_from_date=earliest,
        keys=sum(s.keys for s in summaries),
        movements=movements,
        voucher_count=sum(s.vouchers for s in summaries),
        vouchers=tuple(vouchers),
        locked_periods=tuple(item for s in summaries for item in s.locked),
    )


def _params(branch_id: int, year: FiscalYear, force_from: date | None) -> dict[str, Any]:
    return {
        "branch_id": branch_id,
        "year_start": year.start_date,
        "year_end": year.end_date,
        "force_from": force_from,
    }
