"""Danh sách năm tài chính + kỳ kế toán (`/api/v1/fiscal-years`) — lát 4E.

Đây là "tấm lịch" của mọi màn hình sổ sách: bảng cân đối cần chọn kỳ, số dư
ban đầu cần chọn năm, màn khóa sổ cần thấy kỳ nào đã khóa. Chỉ cần đăng nhập
(`Authorized`), không mã quyền riêng: cấu trúc năm/kỳ không mang số liệu —
cùng mức lộ với việc mọi chứng từ đều hiện `period_id` của nó; số liệu thật
nằm sau các cổng quyền của từng màn hình.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from ket.api.dependencies import Authorized, SessionFactory
from ket.api.routers.period_lock_schemas import PeriodResponse
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work

router = APIRouter(prefix="/api/v1/fiscal-years", tags=["fiscal-years"])


class FiscalYearResponse(BaseModel):
    """Một năm tài chính kèm trọn danh sách kỳ của nó, xếp theo `period_no`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    start_date: date
    end_date: date
    accounting_scheme: str
    base_currency: str
    is_closed: bool
    periods: list[PeriodResponse]


class FiscalYearListResponse(BaseModel):
    """Mọi năm tài chính của dataset, năm mới nhất trước."""

    items: list[FiscalYearResponse]


@router.get("", response_model=FiscalYearListResponse)
def list_fiscal_years(authorized: Authorized, factory: SessionFactory) -> FiscalYearListResponse:
    """Năm + kỳ để các màn hình dựng bộ chọn — `locked_at` cho biết kỳ đã khóa."""
    with unit_of_work(factory, authorized.scope) as session:
        years = (
            session.execute(select(FiscalYear).order_by(FiscalYear.start_date.desc()))
            .scalars()
            .all()
        )
        periods_by_year: dict[int, list[PeriodResponse]] = {}
        for period in session.execute(
            select(AccountingPeriod).order_by(AccountingPeriod.period_no)
        ).scalars():
            periods_by_year.setdefault(period.fiscal_year_id, []).append(
                PeriodResponse.model_validate(period)
            )
        return FiscalYearListResponse(
            items=[
                FiscalYearResponse(
                    id=year.id,
                    code=year.code,
                    start_date=year.start_date,
                    end_date=year.end_date,
                    accounting_scheme=year.accounting_scheme,
                    base_currency=year.base_currency,
                    is_closed=year.is_closed,
                    periods=periods_by_year.get(year.id, []),
                )
                for year in years
            ]
        )
