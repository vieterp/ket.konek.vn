"""Sổ cái qua HTTP (`/api/v1/ledger`) — 4B mang bảng cân đối, 4E mang phát sinh.

Đọc-only: mọi con số ở đây SUY RA từ `gl_postings`/`account_balances`, đường
ghi duy nhất là posting engine (luật phụ thuộc #3). RLS lọc chi nhánh trước khi
mã này chạy; `branch_id` chỉ thu hẹp thêm trong phạm vi đã thấy.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.ledger_schemas import (
    LedgerPostingListResponse,
    LedgerPostingResponse,
    TrialBalanceResponse,
    TrialBalanceRowResponse,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.balances.postings_query import postings_page
from ket.posting.balances.query_service import trial_balance
from ket.posting.balances.recalc_job import BALANCE_VIEW
from ket.posting.engine.models import Ledger

router = APIRouter(prefix="/api/v1/ledger", tags=["ledger"])

BalanceViewer = Annotated[AuthorizedRequest, Depends(require_permission(BALANCE_VIEW))]


@router.get("/trial-balance", response_model=TrialBalanceResponse)
def get_trial_balance(
    authorized: BalanceViewer,
    factory: SessionFactory,
    period_id: Annotated[int, Query(ge=1)],
    ledger: Annotated[int, Query(ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)] = Ledger.FINANCIAL,
    branch_id: Annotated[int | None, Query(ge=1)] = None,
) -> TrialBalanceResponse:
    """Bảng cân đối tài khoản của một (kỳ, sổ) — dư đầu, phát sinh, dư cuối.

    Kỳ còn dấu bẩn thì số tính thẳng từ sổ cái và `stale=true` — client hiện
    chỉ báo "đang chờ tính lại" thay vì âm thầm nhận số chậm hơn bình thường.
    """
    with unit_of_work(factory, authorized.scope) as session:
        result = trial_balance(session, ledger=ledger, period_id=period_id, branch_id=branch_id)
        return TrialBalanceResponse(
            ledger=ledger,
            period_id=period_id,
            branch_id=branch_id,
            stale=result.stale,
            rows=[TrialBalanceRowResponse.model_validate(row) for row in result.rows],
        )


MAX_POSTINGS_PAGE_SIZE = 200


@router.get("/postings", response_model=LedgerPostingListResponse)
def list_postings(
    authorized: BalanceViewer,
    factory: SessionFactory,
    ledger: Annotated[int, Query(ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)] = Ledger.FINANCIAL,
    account_id: Annotated[int | None, Query(ge=1)] = None,
    period_id: Annotated[int | None, Query(ge=1)] = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    branch_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_POSTINGS_PAGE_SIZE)] = 50,
) -> LedgerPostingListResponse:
    """Danh sách phát sinh sổ cái theo bộ lọc, cũ trước — thứ tự đọc sổ.

    Chỉ chứng từ đã ghi sổ có mặt ở đây (N1 — `gl_postings` chỉ tồn tại khi đã
    ghi sổ) nên không cần cờ `stale`. Đây là nền drill-down của bảng cân đối:
    lọc theo `account_id` + `period_id` là "mở một dòng" (U10, phase 10a).
    """
    with unit_of_work(factory, authorized.scope) as session:
        result = postings_page(
            session,
            ledger=ledger,
            account_id=account_id,
            period_id=period_id,
            from_date=from_date,
            to_date=to_date,
            branch_id=branch_id,
            page=page,
            page_size=page_size,
        )
        return LedgerPostingListResponse(
            items=[LedgerPostingResponse.model_validate(row) for row in result.rows],
            total=result.total,
            page=page,
            page_size=page_size,
        )
