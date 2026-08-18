"""Mã quy cách của mã hàng — bảng con của danh mục `items` (FR-SYS-046).

Cùng hình dạng và cùng lập luận tách tệp như `items_units.py`; bối cảnh dùng chung
(mã quyền, nạp mã hàng chủ) ở `items_common.py`.

Giao diện khai quy cách thuộc 3D và báo cáo tồn theo quy cách thuộc phase 8; ở đây
là đường ghi để cả hai có dữ liệu thật mà dựng trên — và để **bảng** ra đời trước
khi phase 8 khóa bảng tồn kho (H65).
"""

from __future__ import annotations

from datetime import timedelta
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
    ItemVariantCreateRequest,
    ItemVariantListResponse,
    ItemVariantResponse,
    ItemVariantUpdateRequest,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.item_variant_service import ItemVariantService
from ket.kernel.persistence.unit_of_work import unit_of_work

PREFIX: Final[str] = f"/api/v1/master/{ITEM_SLUG}/{{item_id}}/variants"

ADD_ROUTE: Final[str] = f"POST /api/v1/master/{ITEM_SLUG}/{{item_id}}/variants"

router = APIRouter(prefix=PREFIX, tags=["master-data"])

AddKey = Annotated[str, Depends(idempotency_key_dependency(ADD_ROUTE))]


@router.get("", response_model=ItemVariantListResponse, summary="Vật tư hàng hóa — mã quy cách")
def list_item_variants(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
    include_inactive: Annotated[bool, Query()] = False,
) -> ItemVariantListResponse:
    """Quy cách của một mã hàng, theo mã.

    **Không** phân trang: một mã hàng có vài chục quy cách nhiều nhất (màu × size),
    không phải vài nghìn.
    """
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        variants = ItemVariantService(session).list_for(item_id, include_inactive=include_inactive)
        return ItemVariantListResponse(
            items=[ItemVariantResponse.model_validate(variant) for variant in variants]
        )


@router.post(
    "",
    response_model=ItemVariantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Vật tư hàng hóa — thêm mã quy cách",
)
def add_item_variant(
    item_id: int,
    payload: ItemVariantCreateRequest,
    authorized: ItemAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddKey,
    response: Response,
) -> ItemVariantResponse:
    """Thêm một mã quy cách — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[ItemVariantResponse, IdempotentRef]:
        load_item(session, item_id, authorized)
        variant = ItemVariantService(session).add(item_id=item_id, code=body.code, name=body.name)
        return ItemVariantResponse.model_validate(variant), IdempotentRef(
            result_type=ItemVariantService(session).entity_type, result_id=str(variant.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ItemVariantResponse:
        load_item(session, item_id, authorized)
        variant = ItemVariantService(session).get(int(ref.result_id), item_id=item_id)
        return ItemVariantResponse.model_validate(variant)

    created_variant, created = execute_once(
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
    return created_variant


@router.put(
    "/{variant_id}",
    response_model=ItemVariantResponse,
    summary="Vật tư hàng hóa — sửa mã quy cách",
)
def update_item_variant(
    item_id: int,
    variant_id: int,
    payload: ItemVariantUpdateRequest,
    authorized: ItemEditor,
    factory: SessionFactory,
) -> ItemVariantResponse:
    """Sửa một quy cách, gồm cả cờ còn dùng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        variant = ItemVariantService(session).update(
            variant_id,
            item_id=item_id,
            expected_row_version=payload.row_version,
            code=payload.code,
            name=payload.name,
            is_active=payload.is_active,
        )
        return ItemVariantResponse.model_validate(variant)


@router.delete(
    "/{variant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Vật tư hàng hóa — xóa mã quy cách",
)
def delete_item_variant(
    item_id: int,
    variant_id: int,
    authorized: ItemRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một quy cách khỏi hồ sơ mã hàng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ItemVariantService(session).delete(variant_id, item_id=item_id)
