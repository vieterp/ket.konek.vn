"""Sổ cái qua HTTP (`/api/v1/ledger`) — slice 4B mang bảng cân đối tài khoản.

Đọc-only: mọi con số ở đây SUY RA từ `gl_postings`/`account_balances`, đường
ghi duy nhất là posting engine (luật phụ thuộc #3). RLS lọc chi nhánh trước khi
mã này chạy; `branch_id` chỉ thu hẹp thêm trong phạm vi đã thấy.

Danh sách phát sinh (`/ledger/postings`) thuộc lát 4E cùng màn hình đọc nó.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.ledger_schemas import TrialBalanceResponse, TrialBalanceRowResponse
from ket.kernel.persistence.unit_of_work import unit_of_work
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
