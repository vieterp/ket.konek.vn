"""Endpoint thủ quỹ (`/api/v1/treasurer/*`) — SRS 17 §3.1, lát 6C.

Màn hình thủ quỹ đúng 3 việc (U6): nhận đề nghị (hàng đợi) → thu/chi tiền thật
→ ghi sổ quỹ. Vai trò Thủ quỹ chỉ cần `treasurer.cash_book.{view,post}` (+
quyền XEM phiếu để đọc chi tiết) — không sửa được chứng từ kế toán vì không có
mã quyền `edit` nào để cấp (FR-WHK-020, BR-WHK-04).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.treasurer_schemas import (
    TreasurerBookRequest,
    TreasurerBookResponse,
    TreasurerCashBookResponse,
    TreasurerCashBookRowOut,
    TreasurerQueueItem,
    TreasurerQueueResponse,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.warehousing.treasurer import (
    TREASURER_CASH_BOOK_CODE,
    TREASURER_PERMISSION_MODULE,
)
from ket.modules.warehousing.treasurer.queue_service import (
    book_vouchers,
    cash_book_rows,
    pending_queue,
)

router = APIRouter(prefix="/api/v1/treasurer", tags=["treasurer"])

TREASURER_VIEW = permission_code(TREASURER_PERMISSION_MODULE, TREASURER_CASH_BOOK_CODE, Action.VIEW)
TREASURER_POST = permission_code(TREASURER_PERMISSION_MODULE, TREASURER_CASH_BOOK_CODE, Action.POST)

TreasurerReader = Annotated[AuthorizedRequest, Depends(require_permission(TREASURER_VIEW))]
TreasurerBooker = Annotated[AuthorizedRequest, Depends(require_permission(TREASURER_POST))]

BOOK_ROUTE: Final[str] = "POST /api/v1/treasurer/queue/actions/book"
BookKey = Annotated[str, Depends(idempotency_key_dependency(BOOK_ROUTE))]


@router.get("/queue", response_model=TreasurerQueueResponse)
def treasurer_queue(authorized: TreasurerReader, factory: SessionFactory) -> TreasurerQueueResponse:
    """Phiếu thu/chi đã ghi sổ kế toán, chờ thủ quỹ (FR-WHK-001) — RLS lọc chi
    nhánh trước khi mã này chạy."""
    with unit_of_work(factory, authorized.scope) as session:
        return TreasurerQueueResponse(
            items=tuple(TreasurerQueueItem.from_pending(row) for row in pending_queue(session))
        )


@router.post("/queue/actions/book", response_model=TreasurerBookResponse)
def book_queue_vouchers(
    payload: TreasurerBookRequest,
    authorized: TreasurerBooker,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: BookKey,
    response: Response,
) -> TreasurerBookResponse:
    """Ghi sổ quỹ hàng loạt (FR-WHK-002/003) — cả lô một transaction, phiếu đầu
    tiên vi phạm (BR-WHK-05, trạng thái) làm cả lượt dừng có thông điệp."""

    def work(session: Session) -> tuple[TreasurerBookResponse, IdempotentRef]:
        entries = book_vouchers(
            session,
            voucher_ids=payload.voucher_ids,
            book_date=payload.book_date,
            user_id=authorized.scope.user_id,
        )
        # Tham chiếu idempotency chỉ chứa được khóa ngắn (String(100)) — kết
        # quả là số phiếu đã ghi, replay trả lại đúng con số đó.
        return TreasurerBookResponse(booked_count=len(entries)), IdempotentRef(
            result_type="treasurer_cash_book", result_id=str(len(entries))
        )

    def replay(session: Session, ref: IdempotentRef) -> TreasurerBookResponse:
        return TreasurerBookResponse(booked_count=int(ref.result_id))

    body, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=BOOK_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_200_OK
    return body


@router.get("/cash-book", response_model=TreasurerCashBookResponse)
def treasurer_cash_book(
    authorized: TreasurerReader,
    factory: SessionFactory,
    cash_account_id: Annotated[int | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
) -> TreasurerCashBookResponse:
    """Sổ quỹ tiền mặt của thủ quỹ (FR-WHK-005) — dòng theo ngày ghi sổ."""
    with unit_of_work(factory, authorized.scope) as session:
        rows = cash_book_rows(
            session,
            cash_account_id=cash_account_id,
            from_date=from_date,
            to_date=to_date,
        )
        return TreasurerCashBookResponse(
            items=tuple(TreasurerCashBookRowOut.model_validate(row) for row in rows)
        )
