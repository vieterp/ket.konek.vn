"""Endpoint thủ kho (`/api/v1/warehouse-keeper/*`) — SRS 17 §3.2, lát 8D.

Màn hình thủ kho đúng 3 việc (U6): nhận đề nghị (hàng đợi) → nhập/xuất hàng
thật → ghi sổ kho. Vai trò Thủ kho chỉ cần `keeper.warehouse_book.{view,post}`
(+ quyền XEM phiếu nhập/xuất để đọc chi tiết) — không sửa được chứng từ kế toán
vì không có mã quyền `edit` nào để cấp (FR-WHK-020).
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
from ket.api.routers.warehouse_keeper_schemas import (
    KeeperBookRequest,
    KeeperBookResponse,
    KeeperQueueItem,
    KeeperQueueResponse,
    WarehouseBookResponse,
    WarehouseBookRowOut,
    WarehouseCardResponse,
    WarehouseCardRow,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.inventory import KEEPER_PERMISSION_MODULE, KEEPER_WAREHOUSE_BOOK_CODE
from ket.modules.inventory.keeper import book_rows, book_vouchers, card_opening_qty, pending_queue

router = APIRouter(prefix="/api/v1/warehouse-keeper", tags=["warehouse-keeper"])

KEEPER_VIEW = permission_code(KEEPER_PERMISSION_MODULE, KEEPER_WAREHOUSE_BOOK_CODE, Action.VIEW)
KEEPER_POST = permission_code(KEEPER_PERMISSION_MODULE, KEEPER_WAREHOUSE_BOOK_CODE, Action.POST)

KeeperReader = Annotated[AuthorizedRequest, Depends(require_permission(KEEPER_VIEW))]
KeeperBooker = Annotated[AuthorizedRequest, Depends(require_permission(KEEPER_POST))]

MAX_PAGE_SIZE: Final[int] = 200

BOOK_ROUTE: Final[str] = "POST /api/v1/warehouse-keeper/queue/actions/book"
BookKey = Annotated[str, Depends(idempotency_key_dependency(BOOK_ROUTE))]


@router.get("/queue", response_model=KeeperQueueResponse)
def keeper_queue(authorized: KeeperReader, factory: SessionFactory) -> KeeperQueueResponse:
    """Phiếu kho đã ghi sổ kế toán, chờ thủ kho (FR-WHK-010) — RLS lọc chi nhánh."""
    with unit_of_work(factory, authorized.scope) as session:
        return KeeperQueueResponse(
            items=tuple(
                KeeperQueueItem(
                    voucher_id=voucher.id,
                    voucher_no=voucher.voucher_no,
                    document_type=voucher.document_type,
                    branch_id=voucher.branch_id,
                    posting_date=voucher.posting_date,
                    warehouse_id=body.warehouse_id,
                    to_warehouse_id=body.to_warehouse_id,
                    delivered_by=body.delivered_by,
                    description=voucher.description,
                )
                for voucher, body in pending_queue(session)
            )
        )


@router.post("/queue/actions/book", response_model=KeeperBookResponse)
def book_queue_vouchers(
    payload: KeeperBookRequest,
    authorized: KeeperBooker,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: BookKey,
    response: Response,
) -> KeeperBookResponse:
    """Ghi sổ kho hàng loạt (FR-WHK-012) — cả lô một transaction, phiếu đầu tiên
    vi phạm (trạng thái, ngày ghi) làm cả lượt dừng có thông điệp nêu đích danh."""

    def work(session: Session) -> tuple[KeeperBookResponse, IdempotentRef]:
        written = book_vouchers(
            session,
            voucher_ids=payload.voucher_ids,
            book_date=payload.book_date,
            user_id=authorized.scope.user_id,
        )
        return KeeperBookResponse(booked_rows=written), IdempotentRef(
            result_type="warehouse_book", result_id=str(written)
        )

    def replay(session: Session, ref: IdempotentRef) -> KeeperBookResponse:
        return KeeperBookResponse(booked_rows=int(ref.result_id))

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


@router.get("/book", response_model=WarehouseBookResponse)
def warehouse_book(
    authorized: KeeperReader,
    factory: SessionFactory,
    warehouse_id: Annotated[int | None, Query()] = None,
    item_id: Annotated[int | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WarehouseBookResponse:
    """Sổ kho của thủ kho (FR-WHK-014) — dòng theo ngày ghi sổ, có phân trang."""
    with unit_of_work(factory, authorized.scope) as session:
        rows, total = book_rows(
            session,
            warehouse_id=warehouse_id,
            item_id=item_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        return WarehouseBookResponse(
            items=tuple(WarehouseBookRowOut.model_validate(row) for row in rows), total=total
        )


@router.get("/card", response_model=WarehouseCardResponse)
def warehouse_card(
    authorized: KeeperReader,
    factory: SessionFactory,
    warehouse_id: Annotated[int, Query()],
    item_id: Annotated[int, Query()],
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WarehouseCardResponse:
    """Thẻ kho (FR-WHK-014): một mã hàng, một kho, kèm tồn lũy kế.

    Tồn lũy kế cộng dồn từ `opening_qty` **trong trang đang xem** — cộng dồn
    phải bắt đầu từ một con số thật, nếu không trang 2 sẽ vẽ một đường tồn bắt
    đầu lại từ 0.
    """
    with unit_of_work(factory, authorized.scope) as session:
        rows, total = book_rows(
            session,
            warehouse_id=warehouse_id,
            item_id=item_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        first = rows[0] if rows else None
        running = card_opening_qty(
            session,
            warehouse_id=warehouse_id,
            item_id=item_id,
            before_date=first.book_date if first is not None else from_date,
            before_id=first.id if first is not None else None,
        )
        opening = running
        card_rows: list[WarehouseCardRow] = []
        for row in rows:
            running = running + row.in_qty - row.out_qty
            card_rows.append(
                WarehouseCardRow(
                    **WarehouseBookRowOut.model_validate(row).model_dump(),
                    running_qty=running,
                )
            )
        return WarehouseCardResponse(
            warehouse_id=warehouse_id,
            item_id=item_id,
            opening_qty=opening,
            items=tuple(card_rows),
            total=total,
        )
