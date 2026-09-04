"""Dòng của bảng giá — bảng con của danh mục `price_lists` (FR-SAL-020).

Cùng hình dạng và cùng lập luận tách tệp như `items_units.py`.

Không có tệp `*_common.py` đi kèm, khác cặp router của vật tư hàng hóa: bối cảnh
dùng chung ở đó ra đời vì **hai** tệp router hỏi cùng ba câu, còn bảng giá chỉ có
một bảng con. Dựng sẵn một tệp dùng chung cho một người dùng là dựng một chỗ nữa
để đọc mà chưa gỡ được bản sao nào.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, AuthorizedRequest, SessionFactory, require_permission
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.master_data_guards import ensure_catalog_choice, ensure_visible
from ket.api.routers.price_list_schemas import (
    PriceListLineCreateRequest,
    PriceListLineListResponse,
    PriceListLineResponse,
    PriceListLineUpdateRequest,
)
from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.price_list_line_service import PriceListLineService
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action

PRICE_LIST_SLUG: Final[str] = "price_lists"
ITEM_SLUG: Final[str] = "items"
UNIT_SLUG: Final[str] = "units_of_measure"


def _price_list_spec() -> CatalogSpec:
    """Mô tả danh mục bảng giá, lấy từ registry — xem `items_common._item_spec`."""
    spec = CATALOG_REGISTRY.get(PRICE_LIST_SLUG)
    if spec is None:  # pragma: no cover - registry nạp lúc import, thiếu là lỗi khởi động
        raise RuntimeError(f"Danh mục {PRICE_LIST_SLUG!r} chưa được đăng ký")
    return spec


SPEC: Final[CatalogSpec] = _price_list_spec()

ListReader = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.VIEW)))
]
ListAuthor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.CREATE)))
]
ListEditor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.EDIT)))
]
ListRemover = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.DELETE)))
]

PREFIX: Final[str] = f"/api/v1/master/{PRICE_LIST_SLUG}/{{price_list_id}}/lines"
ADD_ROUTE: Final[str] = f"POST {PREFIX}"

router = APIRouter(prefix=PREFIX, tags=["master-data"])

AddKey = Annotated[str, Depends(idempotency_key_dependency(ADD_ROUTE))]


def load_price_list(session: Session, price_list_id: int, authorized: AuthorizedRequest) -> None:
    """Bảng giá chủ phải tồn tại, **nhìn thấy được**, và không phải nút nhóm.

    Nhóm bị loại vì nó không bao giờ được áp lên chứng từ — bộ định giá lọc
    `is_group = FALSE` (xem `kernel/pricing._price_list_query`), nên dòng giá của
    một nhóm là dữ liệu không đường nào đọc tới. Cùng lập luận `items_common.load_item`.
    """
    service: MasterDataService[MasterDataRow] = MasterDataService(session, SPEC.model)
    price_list = service.get(price_list_id)
    ensure_visible(price_list, authorized.scope.acting_branch_id, SPEC)
    if price_list.is_group:
        raise MasterDataNotFoundError(
            "Nhóm bảng giá không có dòng giá — hãy chọn một bảng giá cụ thể",
            entity_type=SPEC.entity_type,
            entity_id=price_list_id,
        )


@router.get("", response_model=PriceListLineListResponse, summary="Bảng giá — dòng giá")
def list_price_list_lines(
    price_list_id: int,
    authorized: ListReader,
    factory: SessionFactory,
) -> PriceListLineListResponse:
    """Dòng của một bảng giá — theo mã hàng, đơn vị chính trước, ngưỡng tăng dần.

    **Không** phân trang: màn hình của bảng giá là một lưới sửa tại chỗ, và phân
    trang ở đó là bắt người sửa giá lật trang giữa hai lần gõ — xem
    `PriceListLineService.list_for`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        load_price_list(session, price_list_id, authorized)
        rows = PriceListLineService(session).list_for(price_list_id)
        return PriceListLineListResponse(
            items=[PriceListLineResponse.model_validate(row) for row in rows]
        )


@router.post(
    "",
    response_model=PriceListLineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Bảng giá — thêm dòng giá",
)
def add_price_list_line(
    price_list_id: int,
    payload: PriceListLineCreateRequest,
    authorized: ListAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddKey,
    response: Response,
) -> PriceListLineResponse:
    """Thêm một dòng giá — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[PriceListLineResponse, IdempotentRef]:
        load_price_list(session, price_list_id, authorized)
        ensure_catalog_choice(session, ITEM_SLUG, body.item_id, authorized)
        if body.unit_id is not None:
            ensure_catalog_choice(session, UNIT_SLUG, body.unit_id, authorized)
        row = PriceListLineService(session).add(
            price_list_id=price_list_id,
            item_id=body.item_id,
            unit_id=body.unit_id,
            min_quantity=body.min_quantity,
            price=body.price,
        )
        return PriceListLineResponse.model_validate(row), IdempotentRef(
            result_type=PriceListLineService(session).entity_type, result_id=str(row.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> PriceListLineResponse:
        load_price_list(session, price_list_id, authorized)
        row = PriceListLineService(session).get(int(ref.result_id), price_list_id=price_list_id)
        return PriceListLineResponse.model_validate(row)

    created_row, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_ROUTE,
        key=idempotency_key,
        # `price_list_id` vào **vân tay** — xem lập luận M-6 ở `items_units.py`.
        fingerprint=fingerprint_of(f"{price_list_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_row


@router.put("/{row_id}", response_model=PriceListLineResponse, summary="Bảng giá — sửa dòng giá")
def update_price_list_line(
    price_list_id: int,
    row_id: int,
    payload: PriceListLineUpdateRequest,
    authorized: ListEditor,
    factory: SessionFactory,
) -> PriceListLineResponse:
    """Sửa mã hàng, đơn vị, ngưỡng hoặc đơn giá của một dòng."""
    with unit_of_work(factory, authorized.scope) as session:
        load_price_list(session, price_list_id, authorized)
        ensure_catalog_choice(session, ITEM_SLUG, payload.item_id, authorized)
        if payload.unit_id is not None:
            ensure_catalog_choice(session, UNIT_SLUG, payload.unit_id, authorized)
        row = PriceListLineService(session).update(
            row_id,
            price_list_id=price_list_id,
            expected_row_version=payload.row_version,
            item_id=payload.item_id,
            unit_id=payload.unit_id,
            min_quantity=payload.min_quantity,
            price=payload.price,
        )
        return PriceListLineResponse.model_validate(row)


@router.delete(
    "/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Bảng giá — xóa dòng giá",
)
def delete_price_list_line(
    price_list_id: int,
    row_id: int,
    authorized: ListRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một dòng khỏi bảng giá."""
    with unit_of_work(factory, authorized.scope) as session:
        load_price_list(session, price_list_id, authorized)
        PriceListLineService(session).delete(row_id, price_list_id=price_list_id)
