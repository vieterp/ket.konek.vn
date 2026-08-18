"""Đơn vị quy đổi của mã hàng — bảng con của danh mục `items` (FR-SYS-041).

Tách khỏi bộ sinh route ở `routers/master_data.py` vì hình dạng khác hẳn: đây là
danh sách **thuộc về** một bản ghi danh mục, không phải một danh mục. Nới bộ sinh
để nó biết thêm khái niệm "bảng con" sẽ bắt mười chín danh mục còn lại mang theo
một khả năng chúng không có — đúng lối "bẻ cong khung dùng chung" mà §Risk của
phase 3 cảnh báo.

Phạm vi chi nhánh và mã quyền đi qua **mã hàng chủ** (`items_common.py`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, SessionFactory
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.items_common import (
    ITEM_SLUG,
    UNIT_SLUG,
    ItemAuthor,
    ItemEditor,
    ItemReader,
    ItemRemover,
    load_item,
)
from ket.api.routers.items_schemas import (
    ItemUnitCreateRequest,
    ItemUnitListResponse,
    ItemUnitResponse,
    ItemUnitUpdateRequest,
)
from ket.api.routers.master_data_guards import ensure_catalog_choice
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.item_unit_service import ItemUnitService
from ket.kernel.persistence.unit_of_work import unit_of_work

PREFIX: Final[str] = f"/api/v1/master/{ITEM_SLUG}/{{item_id}}/units"

ADD_ROUTE: Final[str] = f"POST /api/v1/master/{ITEM_SLUG}/{{item_id}}/units"

router = APIRouter(prefix=PREFIX, tags=["master-data"])

AddKey = Annotated[str, Depends(idempotency_key_dependency(ADD_ROUTE))]


@router.get("", response_model=ItemUnitListResponse, summary="Vật tư hàng hóa — đơn vị quy đổi")
def list_item_units(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
) -> ItemUnitListResponse:
    """Đơn vị quy đổi của một mã hàng, tỷ lệ lớn trước.

    **Không** phân trang, khác đường đọc danh mục: một mã hàng có vài đơn vị, không
    phải vài nghìn. Thêm phân trang ở đây là thêm hai tham số mà mọi màn hình sẽ
    truyền giá trị mặc định.
    """
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        rows = ItemUnitService(session).list_for(item_id)
        return ItemUnitListResponse(items=[ItemUnitResponse.model_validate(row) for row in rows])


@router.post(
    "",
    response_model=ItemUnitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Vật tư hàng hóa — thêm đơn vị quy đổi",
)
def add_item_unit(
    item_id: int,
    payload: ItemUnitCreateRequest,
    authorized: ItemAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddKey,
    response: Response,
) -> ItemUnitResponse:
    """Thêm một đơn vị quy đổi — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[ItemUnitResponse, IdempotentRef]:
        load_item(session, item_id, authorized)
        ensure_catalog_choice(session, UNIT_SLUG, body.unit_id, authorized)
        row = ItemUnitService(session).add(
            item_id=item_id, unit_id=body.unit_id, factor=body.factor
        )
        return ItemUnitResponse.model_validate(row), IdempotentRef(
            result_type=ItemUnitService(session).entity_type, result_id=str(row.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ItemUnitResponse:
        load_item(session, item_id, authorized)
        row = ItemUnitService(session).get(int(ref.result_id), item_id=item_id)
        return ItemUnitResponse.model_validate(row)

    created_row, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_ROUTE,
        key=idempotency_key,
        # `item_id` vào **vân tay**, không chỉ trong đường dẫn: `route_key` chứa
        # `{item_id}` theo nghĩa đen nên cùng một khóa gửi lên **mã hàng khác**
        # với cùng thân request sẽ bị coi là lượt phát lại, và `replay` đi tìm
        # dòng cũ dưới mã hàng mới rồi trả `404` — dòng không bao giờ được tạo
        # (review M-6). Có `item_id` trong vân tay thì đó là `409` "khóa đã dùng
        # cho yêu cầu khác", tức đúng câu trả lời và đúng cách sửa.
        fingerprint=fingerprint_of(f"{item_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_row


@router.put(
    "/{row_id}", response_model=ItemUnitResponse, summary="Vật tư hàng hóa — sửa đơn vị quy đổi"
)
def update_item_unit(
    item_id: int,
    row_id: int,
    payload: ItemUnitUpdateRequest,
    authorized: ItemEditor,
    factory: SessionFactory,
) -> ItemUnitResponse:
    """Sửa đơn vị hoặc tỷ lệ của một dòng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ensure_catalog_choice(session, UNIT_SLUG, payload.unit_id, authorized)
        row = ItemUnitService(session).update(
            row_id,
            item_id=item_id,
            expected_row_version=payload.row_version,
            unit_id=payload.unit_id,
            factor=payload.factor,
        )
        return ItemUnitResponse.model_validate(row)


@router.delete(
    "/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Vật tư hàng hóa — xóa đơn vị quy đổi",
)
def delete_item_unit(
    item_id: int,
    row_id: int,
    authorized: ItemRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một đơn vị quy đổi khỏi hồ sơ mã hàng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ItemUnitService(session).delete(row_id, item_id=item_id)
