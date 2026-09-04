"""Mức giá và bậc chiết khấu của mã hàng — hai bảng con nữa của `items`.

Cùng hình dạng và cùng lập luận tách tệp như `items_units.py`: đây là danh sách
**thuộc về** một bản ghi danh mục, không phải một danh mục, nên chúng không đi qua
bộ sinh route của `routers/master_data.py`. Bối cảnh dùng chung (nạp mã hàng chủ,
phạm vi chi nhánh, mã quyền) lấy ở `items_common.py`.

Hai bảng ở **một** tệp chứ không hai, khác cặp `items_units`/`items_variants`:
giá và chiết khấu là hai nửa của cùng một màn hình khai báo giá bán, và chúng ra
đời cùng lát. Tách đôi ở đây chỉ tạo thêm một tệp mà người sửa phải mở cả hai.
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
    ItemDiscountTierCreateRequest,
    ItemDiscountTierListResponse,
    ItemDiscountTierResponse,
    ItemDiscountTierUpdateRequest,
    ItemPriceLevelCreateRequest,
    ItemPriceLevelListResponse,
    ItemPriceLevelResponse,
    ItemPriceLevelUpdateRequest,
)
from ket.api.routers.master_data_guards import ensure_catalog_choice
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.item_discount_tier_service import ItemDiscountTierService
from ket.kernel.master_data.item_price_level_service import ItemPriceLevelService
from ket.kernel.persistence.unit_of_work import unit_of_work

PRICE_PREFIX: Final[str] = f"/api/v1/master/{ITEM_SLUG}/{{item_id}}/prices"
TIER_PREFIX: Final[str] = f"/api/v1/master/{ITEM_SLUG}/{{item_id}}/discount-tiers"

ADD_PRICE_ROUTE: Final[str] = f"POST {PRICE_PREFIX}"
ADD_TIER_ROUTE: Final[str] = f"POST {TIER_PREFIX}"

router = APIRouter(tags=["master-data"])

AddPriceKey = Annotated[str, Depends(idempotency_key_dependency(ADD_PRICE_ROUTE))]
AddTierKey = Annotated[str, Depends(idempotency_key_dependency(ADD_TIER_ROUTE))]


# ------------------------------------------------------------------ mức giá


@router.get(
    PRICE_PREFIX,
    response_model=ItemPriceLevelListResponse,
    summary="Vật tư hàng hóa — bảng giá nhiều mức",
)
def list_item_prices(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
) -> ItemPriceLevelListResponse:
    """Mức giá của một mã hàng — mua trước bán, đơn vị chính trước, mức tăng dần.

    **Không** phân trang: một mã hàng có vài mức giá, không phải vài nghìn — cùng
    lập luận `list_item_units`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        rows = ItemPriceLevelService(session).list_for(item_id)
        return ItemPriceLevelListResponse(
            items=[ItemPriceLevelResponse.model_validate(row) for row in rows]
        )


@router.post(
    PRICE_PREFIX,
    response_model=ItemPriceLevelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Vật tư hàng hóa — thêm mức giá",
)
def add_item_price(
    item_id: int,
    payload: ItemPriceLevelCreateRequest,
    authorized: ItemAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddPriceKey,
    response: Response,
) -> ItemPriceLevelResponse:
    """Thêm một mức giá — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[ItemPriceLevelResponse, IdempotentRef]:
        load_item(session, item_id, authorized)
        if body.unit_id is not None:
            ensure_catalog_choice(session, UNIT_SLUG, body.unit_id, authorized)
        row = ItemPriceLevelService(session).add(
            item_id=item_id,
            unit_id=body.unit_id,
            direction=body.direction,
            level=body.level,
            price=body.price,
            label=body.label,
        )
        return ItemPriceLevelResponse.model_validate(row), IdempotentRef(
            result_type=ItemPriceLevelService(session).entity_type, result_id=str(row.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ItemPriceLevelResponse:
        load_item(session, item_id, authorized)
        row = ItemPriceLevelService(session).get(int(ref.result_id), item_id=item_id)
        return ItemPriceLevelResponse.model_validate(row)

    created_row, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_PRICE_ROUTE,
        key=idempotency_key,
        # `item_id` vào **vân tay** — xem lập luận M-6 ở `items_units.py`: cùng một
        # khóa gửi lên mã hàng khác với cùng thân request phải là `409`, không phải
        # một lượt phát lại đi tìm dòng cũ dưới mã hàng mới.
        fingerprint=fingerprint_of(f"{item_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_row


@router.put(
    f"{PRICE_PREFIX}/{{row_id}}",
    response_model=ItemPriceLevelResponse,
    summary="Vật tư hàng hóa — sửa mức giá",
)
def update_item_price(
    item_id: int,
    row_id: int,
    payload: ItemPriceLevelUpdateRequest,
    authorized: ItemEditor,
    factory: SessionFactory,
) -> ItemPriceLevelResponse:
    """Sửa đơn vị, chiều, mức, đơn giá hoặc tên thang của một dòng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        if payload.unit_id is not None:
            ensure_catalog_choice(session, UNIT_SLUG, payload.unit_id, authorized)
        row = ItemPriceLevelService(session).update(
            row_id,
            item_id=item_id,
            expected_row_version=payload.row_version,
            unit_id=payload.unit_id,
            direction=payload.direction,
            level=payload.level,
            price=payload.price,
            label=payload.label,
        )
        return ItemPriceLevelResponse.model_validate(row)


@router.delete(
    f"{PRICE_PREFIX}/{{row_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Vật tư hàng hóa — xóa mức giá",
)
def delete_item_price(
    item_id: int,
    row_id: int,
    authorized: ItemRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một mức giá khỏi hồ sơ mã hàng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ItemPriceLevelService(session).delete(row_id, item_id=item_id)


# ---------------------------------------------------------- bậc chiết khấu


@router.get(
    TIER_PREFIX,
    response_model=ItemDiscountTierListResponse,
    summary="Vật tư hàng hóa — bậc chiết khấu theo số lượng",
)
def list_item_discount_tiers(
    item_id: int,
    authorized: ItemReader,
    factory: SessionFactory,
) -> ItemDiscountTierListResponse:
    """Bậc chiết khấu của một mã hàng, ngưỡng tăng dần."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        rows = ItemDiscountTierService(session).list_for(item_id)
        return ItemDiscountTierListResponse(
            items=[ItemDiscountTierResponse.model_validate(row) for row in rows]
        )


@router.post(
    TIER_PREFIX,
    response_model=ItemDiscountTierResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Vật tư hàng hóa — thêm bậc chiết khấu",
)
def add_item_discount_tier(
    item_id: int,
    payload: ItemDiscountTierCreateRequest,
    authorized: ItemAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddTierKey,
    response: Response,
) -> ItemDiscountTierResponse:
    """Thêm một bậc chiết khấu — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[ItemDiscountTierResponse, IdempotentRef]:
        load_item(session, item_id, authorized)
        row = ItemDiscountTierService(session).add(
            item_id=item_id,
            min_quantity=body.min_quantity,
            discount_percent=body.discount_percent,
        )
        return ItemDiscountTierResponse.model_validate(row), IdempotentRef(
            result_type=ItemDiscountTierService(session).entity_type, result_id=str(row.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ItemDiscountTierResponse:
        load_item(session, item_id, authorized)
        row = ItemDiscountTierService(session).get(int(ref.result_id), item_id=item_id)
        return ItemDiscountTierResponse.model_validate(row)

    created_row, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_TIER_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{item_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_row


@router.put(
    f"{TIER_PREFIX}/{{row_id}}",
    response_model=ItemDiscountTierResponse,
    summary="Vật tư hàng hóa — sửa bậc chiết khấu",
)
def update_item_discount_tier(
    item_id: int,
    row_id: int,
    payload: ItemDiscountTierUpdateRequest,
    authorized: ItemEditor,
    factory: SessionFactory,
) -> ItemDiscountTierResponse:
    """Sửa ngưỡng hoặc tỷ lệ của một bậc."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        row = ItemDiscountTierService(session).update(
            row_id,
            item_id=item_id,
            expected_row_version=payload.row_version,
            min_quantity=payload.min_quantity,
            discount_percent=payload.discount_percent,
        )
        return ItemDiscountTierResponse.model_validate(row)


@router.delete(
    f"{TIER_PREFIX}/{{row_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Vật tư hàng hóa — xóa bậc chiết khấu",
)
def delete_item_discount_tier(
    item_id: int,
    row_id: int,
    authorized: ItemRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một bậc chiết khấu khỏi hồ sơ mã hàng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_item(session, item_id, authorized)
        ItemDiscountTierService(session).delete(row_id, item_id=item_id)
