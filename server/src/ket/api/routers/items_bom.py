"""Định mức NVL của mã hàng — bảng con của danh mục `items` (FR-SYS-044, 8C-2).

Cùng hình dạng và cùng lập luận tách tệp như `items_units.py`. Thêm một đường
đọc `GET …/bom/explode?quantity=`: nổ định mức một cấp thành dòng linh kiện gợi
ý cho phiếu lắp ráp / tháo dỡ — client lấy gợi ý rồi gửi phiếu **trọn dòng**
(người lập được lệch định mức; báo cáo so thực xuất với định mức cần thấy lệch).

Phạm vi chi nhánh và mã quyền đi qua **mã hàng chủ** (`items_common.py`).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, SessionFactory
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.items_common import (
    ITEM_SLUG,
    ItemAuthor,
    ItemEditor,
    ItemReader,
    ItemRemover,
    load_item,
)
from ket.api.routers.items_schemas import (
    ItemBomExplodedLine,
    ItemBomExplodeResponse,
    ItemBomLineCreateRequest,
    ItemBomLineListResponse,
    ItemBomLineResponse,
    ItemBomLineUpdateRequest,
)
from ket.api.routers.master_data_guards import ensure_catalog_choice
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.item_bom_service import ItemBomService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

PREFIX: Final[str] = f"/api/v1/master/{ITEM_SLUG}/{{item_id}}/bom"

ADD_ROUTE: Final[str] = f"POST /api/v1/master/{ITEM_SLUG}/{{item_id}}/bom"

router = APIRouter(prefix=PREFIX, tags=["master-data"])

AddKey = Annotated[str, Depends(idempotency_key_dependency(ADD_ROUTE))]


@router.get("", response_model=ItemBomLineListResponse, summary="Vật tư hàng hóa — định mức NVL")
def list_item_bom(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
) -> ItemBomLineListResponse:
    """Định mức của một mã hàng, theo thứ tự khai. Không phân trang — vài dòng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        rows = ItemBomService(session).list_for(item_id)
        return ItemBomLineListResponse(
            items=[ItemBomLineResponse.model_validate(row) for row in rows]
        )


@router.get(
    "/explode",
    response_model=ItemBomExplodeResponse,
    summary="Vật tư hàng hóa — nổ định mức cho một số lượng thành phẩm",
)
def explode_item_bom(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
    quantity: Annotated[
        Decimal, Query(gt=0, max_digits=QUANTITY_PRECISION, decimal_places=QUANTITY_SCALE)
    ] = Decimal(1),
) -> ItemBomExplodeResponse:
    """Dòng linh kiện gợi ý cho `quantity` đơn vị **chính** thành phẩm, số lượng
    theo đơn vị chính của từng linh kiện."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        lines = ItemBomService(session).explode(item_id, quantity)
        return ItemBomExplodeResponse(
            item_id=item_id,
            quantity=quantity,
            lines=[
                ItemBomExplodedLine(
                    component_item_id=line.component_item_id,
                    unit_id=line.unit_id,
                    quantity=line.quantity,
                    allocation_ratio=line.allocation_ratio,
                )
                for line in lines
            ],
        )


@router.post(
    "",
    response_model=ItemBomLineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Vật tư hàng hóa — thêm dòng định mức",
)
def add_item_bom_line(
    item_id: int,
    payload: ItemBomLineCreateRequest,
    authorized: ItemAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddKey,
    response: Response,
) -> ItemBomLineResponse:
    """Thêm một linh kiện vào định mức — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[ItemBomLineResponse, IdempotentRef]:
        load_item(session, item_id, authorized)
        ensure_catalog_choice(session, ITEM_SLUG, body.component_item_id, authorized)
        row = ItemBomService(session).add(
            item_id=item_id,
            component_item_id=body.component_item_id,
            quantity=body.quantity,
            allocation_ratio=body.allocation_ratio,
        )
        return ItemBomLineResponse.model_validate(row), IdempotentRef(
            result_type=ItemBomService(session).entity_type, result_id=str(row.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ItemBomLineResponse:
        load_item(session, item_id, authorized)
        row = ItemBomService(session).get(int(ref.result_id), item_id=item_id)
        return ItemBomLineResponse.model_validate(row)

    created_row, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_ROUTE,
        key=idempotency_key,
        # `item_id` vào vân tay — xem lập luận M-6 ở `items_units.py`.
        fingerprint=fingerprint_of(f"{item_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_row


@router.put(
    "/{row_id}", response_model=ItemBomLineResponse, summary="Vật tư hàng hóa — sửa dòng định mức"
)
def update_item_bom_line(
    item_id: int,
    row_id: int,
    payload: ItemBomLineUpdateRequest,
    authorized: ItemEditor,
    factory: SessionFactory,
) -> ItemBomLineResponse:
    """Sửa linh kiện, số lượng hoặc tỷ lệ của một dòng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ensure_catalog_choice(session, ITEM_SLUG, payload.component_item_id, authorized)
        row = ItemBomService(session).update(
            row_id,
            item_id=item_id,
            expected_row_version=payload.row_version,
            component_item_id=payload.component_item_id,
            quantity=payload.quantity,
            allocation_ratio=payload.allocation_ratio,
        )
        return ItemBomLineResponse.model_validate(row)


@router.delete(
    "/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Vật tư hàng hóa — xóa dòng định mức",
)
def delete_item_bom_line(
    item_id: int,
    row_id: int,
    authorized: ItemRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một dòng khỏi định mức."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ItemBomService(session).delete(row_id, item_id=item_id)
