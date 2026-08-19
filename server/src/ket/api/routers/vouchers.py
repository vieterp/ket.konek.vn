"""Endpoint chứng từ dùng chung (`/api/v1/vouchers`) — phase-04 §API surface.

Một bộ endpoint cho **mọi** loại chứng từ: danh sách, ghi sổ, bỏ ghi sổ, xóa.
Loại chứng từ quyết định mã quyền và cách dựng định khoản — tra qua registry
của posting (`documents/registry.py`), nên phase 6 thêm phiếu thu/chi là thêm
một dòng đăng ký, không thêm endpoint.

Quyền kiểm **bên trong** `execute_once`/transaction chứ không ở dependency chữ
ký: mã quyền phụ thuộc `document_type` của chứng từ, thứ chỉ biết sau khi đọc
DB. Ném trong `work` thì khóa idempotency rollback theo — không có khóa mồ côi
cho một hành động bị từ chối.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    Authorized,
    AuthorizedRequest,
    SessionFactory,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.vouchers_schemas import VoucherListResponse, VoucherResponse
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action
from ket.posting.documents.models import Voucher
from ket.posting.documents.registry import REGISTRY, PostingDocumentType
from ket.posting.documents.service import VoucherService
from ket.posting.engine.service import PostingService

router = APIRouter(prefix="/api/v1/vouchers", tags=["vouchers"])

MAX_PAGE_SIZE: Final[int] = 200

POST_ROUTE: Final[str] = "POST /api/v1/vouchers/{voucher_id}/actions/post"
UNPOST_ROUTE: Final[str] = "POST /api/v1/vouchers/{voucher_id}/actions/unpost"

PostKey = Annotated[str, Depends(idempotency_key_dependency(POST_ROUTE))]
UnpostKey = Annotated[str, Depends(idempotency_key_dependency(UNPOST_ROUTE))]


def _document_type_of(session: Session, voucher_id: UUID) -> tuple[Voucher, PostingDocumentType]:
    voucher = VoucherService(session).require(voucher_id)
    return voucher, REGISTRY.get(voucher.document_type)


@router.get("", response_model=VoucherListResponse)
def list_vouchers(
    authorized: Authorized,
    factory: SessionFactory,
    document_type: Annotated[str | None, Query(alias="type")] = None,
    period_id: Annotated[int | None, Query()] = None,
    voucher_status: Annotated[int | None, Query(alias="status")] = None,
    branch_id: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> VoucherListResponse:
    """Danh sách chứng từ, lọc theo loại/kỳ/trạng thái/chi nhánh, mới nhất trước.

    Không nêu `type` thì trả về chứng từ của **những loại người gọi được xem**
    — mỗi loại một mã quyền `view`, và danh sách trộn không được là đường vòng
    qua phân quyền của một loại. RLS đã lọc chi nhánh trước khi mã này chạy.
    """
    if document_type is not None:
        allowed_types = [document_type]
        authorized.access.require(REGISTRY.get(document_type).permission(Action.VIEW))
    else:
        allowed_types = [
            code
            for code in REGISTRY.codes()
            if authorized.access.has(REGISTRY.get(code).permission(Action.VIEW))
        ]
        if not allowed_types:
            return VoucherListResponse(items=[], total=0, page=page, page_size=page_size)

    query = select(Voucher).where(Voucher.document_type.in_(allowed_types))
    if period_id is not None:
        query = query.where(Voucher.period_id == period_id)
    if voucher_status is not None:
        query = query.where(Voucher.status == voucher_status)
    if branch_id is not None:
        query = query.where(Voucher.branch_id == branch_id)

    with unit_of_work(factory, authorized.scope) as session:
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = (
            session.execute(
                query.order_by(Voucher.posting_date.desc(), Voucher.voucher_no.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return VoucherListResponse(
            items=[VoucherResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )


def _run_action(
    *,
    action: Action,
    route_key: str,
    voucher_id: UUID,
    authorized: AuthorizedRequest,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: str,
    response: Response,
) -> VoucherResponse:
    """Khung chung của post/unpost — chỉ khác mã hành vi và lời gọi service."""

    def work(session: Session) -> tuple[VoucherResponse, IdempotentRef]:
        voucher, document_type = _document_type_of(session, voucher_id)
        authorized.access.require(document_type.permission(action))
        posting = PostingService(session)
        if action is Action.POST:
            request = document_type.build_request(session, voucher_id)
            voucher = posting.post(request, user_id=authorized.scope.user_id)
        else:
            voucher = posting.unpost(voucher_id, user_id=authorized.scope.user_id)
        return VoucherResponse.model_validate(voucher), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> VoucherResponse:
        voucher, document_type = _document_type_of(session, UUID(ref.result_id))
        authorized.access.require(document_type.permission(Action.VIEW))
        return VoucherResponse.model_validate(voucher)

    result, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=route_key,
        key=idempotency_key,
        # Vân tay kèm id: phạm vi khóa là route khai báo (chưa điền id), nên
        # cùng khóa cho hai chứng từ khác nhau phải bị coi là dùng sai chứ
        # không phải "gửi lại" — cùng lý do với `add_dimension_value`.
        fingerprint=fingerprint_of(f"{route_key}:{voucher_id}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    # Hành động trả `200` cho cả lần đầu lẫn lần gửi lại: không có bản ghi mới
    # được tạo, chỉ trạng thái đổi — phân biệt 200/201 là việc của đường tạo.
    response.status_code = status.HTTP_200_OK
    return result


@router.post("/{voucher_id}/actions/post", response_model=VoucherResponse)
def post_voucher(
    voucher_id: UUID,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: PostKey,
    response: Response,
) -> VoucherResponse:
    """Ghi sổ một chứng từ Đã cất (SRS 00 §3.3, FR-NFR-003)."""
    return _run_action(
        action=Action.POST,
        route_key=POST_ROUTE,
        voucher_id=voucher_id,
        authorized=authorized,
        factory=factory,
        settings=settings,
        idempotency_key=idempotency_key,
        response=response,
    )


@router.post("/{voucher_id}/actions/unpost", response_model=VoucherResponse)
def unpost_voucher(
    voucher_id: UUID,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: UnpostKey,
    response: Response,
) -> VoucherResponse:
    """Bỏ ghi sổ — chứng từ về Đã cất, dòng sổ bị xóa (không phải bút toán đảo)."""
    return _run_action(
        action=Action.UNPOST,
        route_key=UNPOST_ROUTE,
        voucher_id=voucher_id,
        authorized=authorized,
        factory=factory,
        settings=settings,
        idempotency_key=idempotency_key,
        response=response,
    )


@router.delete("/{voucher_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_voucher(
    voucher_id: UUID,
    authorized: Authorized,
    factory: SessionFactory,
) -> None:
    """Xóa chứng từ Đã cất. Lặp lại cho `404` — tự nhiên đã idempotent.

    Số chứng từ không tái sử dụng (máy trạng thái); bảng chi tiết module đi
    theo `ON DELETE CASCADE`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        _, document_type = _document_type_of(session, voucher_id)
        authorized.access.require(document_type.permission(Action.DELETE))
        VoucherService(session).delete(voucher_id)
