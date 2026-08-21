"""Endpoint phiếu thu/chi tiền mặt (`/api/v1/cash-book/*`) — SRS 03, lát 6B.

Tạo/sửa/đọc thân phiếu + picker công nợ + biên bản kiểm kê đi qua router của
module (màn hình đọc một module — không BFF, RT-21); ghi sổ / bỏ ghi sổ / xóa
dùng endpoint chứng từ dùng chung (`routers/vouchers.py`) — PT/PC đã đăng ký
loại + hook vòng đời vào registry của posting nên bên đó tự biết kiểm quyền
nào, dựng định khoản ra sao và cộng/gỡ đối trừ khi nào.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    Authorized,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import BranchNotInScopeError, SettlementSideMismatchError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.cash_book import (
    CASH_PERMISSION_MODULE,
    COUNT_SHEET_PERMISSION_CODE,
    PAYMENT_PERMISSION_CODE,
    RECEIPT_PERMISSION_CODE,
)
from ket.modules.cash_book.count_sheet_service import CashCountSheetService
from ket.modules.cash_book.models import (
    CashSettlement,
    CashVoucher,
    CashVoucherKind,
    CashVoucherLine,
)
from ket.modules.cash_book.schemas import (
    CashSettlementOut,
    CashVoucherIn,
    CashVoucherLineOut,
    CashVoucherOut,
    CashVoucherUpdate,
    CountSheetIn,
    CountSheetLineOut,
    CountSheetListResponse,
    CountSheetOut,
    OpenInvoiceOut,
    OpenInvoicesResponse,
)
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.cash_book.settlement_service import open_invoices
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/cash-book", tags=["cash-book"])

RECEIPT_CREATE = permission_code(CASH_PERMISSION_MODULE, RECEIPT_PERMISSION_CODE, Action.CREATE)
PAYMENT_CREATE = permission_code(CASH_PERMISSION_MODULE, PAYMENT_PERMISSION_CODE, Action.CREATE)
RECEIPT_VIEW = permission_code(CASH_PERMISSION_MODULE, RECEIPT_PERMISSION_CODE, Action.VIEW)
PAYMENT_VIEW = permission_code(CASH_PERMISSION_MODULE, PAYMENT_PERMISSION_CODE, Action.VIEW)

COUNT_SHEET_VIEW = permission_code(CASH_PERMISSION_MODULE, COUNT_SHEET_PERMISSION_CODE, Action.VIEW)
COUNT_SHEET_CREATE = permission_code(
    CASH_PERMISSION_MODULE, COUNT_SHEET_PERMISSION_CODE, Action.CREATE
)
COUNT_SHEET_DELETE = permission_code(
    CASH_PERMISSION_MODULE, COUNT_SHEET_PERMISSION_CODE, Action.DELETE
)

CountSheetReader = Annotated[AuthorizedRequest, Depends(require_permission(COUNT_SHEET_VIEW))]
CountSheetAuthor = Annotated[AuthorizedRequest, Depends(require_permission(COUNT_SHEET_CREATE))]
CountSheetRemover = Annotated[AuthorizedRequest, Depends(require_permission(COUNT_SHEET_DELETE))]

CREATE_ROUTE: Final[str] = "POST /api/v1/cash-book/vouchers"
COUNT_SHEET_ROUTE: Final[str] = "POST /api/v1/cash-book/count-sheets"
ADJUSTMENT_ROUTE: Final[str] = (
    "POST /api/v1/cash-book/count-sheets/{sheet_id}/actions/create-adjustment"
)
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]
CountSheetKey = Annotated[str, Depends(idempotency_key_dependency(COUNT_SHEET_ROUTE))]
AdjustmentKey = Annotated[str, Depends(idempotency_key_dependency(ADJUSTMENT_ROUTE))]

MAX_PAGE_SIZE: Final[int] = 200


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _kind_permissions(kind: int) -> tuple[str, str]:
    """(create, view) theo loại phiếu — PT và PC là hai mã quyền riêng."""
    if kind == CashVoucherKind.RECEIPT:
        return RECEIPT_CREATE, RECEIPT_VIEW
    return PAYMENT_CREATE, PAYMENT_VIEW


def _to_response(
    voucher: Voucher,
    body: CashVoucher,
    lines: list[CashVoucherLine],
    settlements: list[CashSettlement],
) -> CashVoucherOut:
    base = CashVoucherOut.model_validate(voucher)
    return base.model_copy(
        update={
            "kind": body.kind,
            "operation_code": body.operation_code,
            "cash_account_id": body.cash_account_id,
            "partner_id": body.partner_id,
            "partner_kind": body.partner_kind,
            "payer_receiver_name": body.payer_receiver_name,
            "attachment_count": body.attachment_count,
            "treasurer_status": body.treasurer_status,
            "lines": tuple(CashVoucherLineOut.model_validate(line) for line in lines),
            "settlements": tuple(CashSettlementOut.model_validate(row) for row in settlements),
        }
    )


@router.post("/vouchers", response_model=CashVoucherOut, status_code=status.HTTP_201_CREATED)
def create_cash_voucher(
    payload: CashVoucherIn,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> CashVoucherOut:
    """Cất phiếu thu/chi; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction.

    `acknowledge_warnings` chỉ có tác dụng trên lượt ghi sổ đi kèm đó (FR-SYS-062
    mức "Cảnh báo" — ví dụ chi quá tồn quỹ); mức "Chặn" không mở được.
    """
    _require_branch_in_scope(authorized, payload.branch_id)
    create_permission, _ = _kind_permissions(payload.kind)
    authorized.access.require(create_permission)

    def work(session: Session) -> tuple[CashVoucherOut, IdempotentRef]:
        service = CashVoucherService(session)
        if value_of(session, key=SAVE_ALSO_POSTS_KEY, user_id=authorized.scope.user_id) is True:
            permission_name = (
                RECEIPT_PERMISSION_CODE
                if payload.kind == CashVoucherKind.RECEIPT
                else PAYMENT_PERMISSION_CODE
            )
            authorized.access.require(
                permission_code(CASH_PERMISSION_MODULE, permission_name, Action.POST)
            )
        voucher = service.create(
            payload,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledge_warnings,
        )
        header, body, lines, settlements = service.get(voucher.id)
        return _to_response(header, body, lines, settlements), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> CashVoucherOut:
        header, body, lines, settlements = CashVoucherService(session).get(UUID(ref.result_id))
        return _to_response(header, body, lines, settlements)

    created_body, created = execute_once(
        factory,
        authorized.scope,
        route_key=CREATE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_body


@router.get("/open-invoices", response_model=OpenInvoicesResponse)
def list_open_invoices(
    authorized: Authorized,
    factory: SessionFactory,
    side: Annotated[Literal["receivable", "payable"], Query()],
    partner_kind: Annotated[int, Query(ge=0, le=2)],
    partner_id: Annotated[int, Query()],
    branch_id: Annotated[int, Query()],
    as_of: Annotated[date, Query()],
) -> OpenInvoicesResponse:
    """Chứng từ công nợ còn nợ của một đối tác cho picker đối trừ (`docs/srs/03` §4).

    Phía phải thu đòi quyền xem phiếu thu, phải trả đòi quyền xem phiếu chi —
    danh sách này tồn tại để lập đúng loại phiếu đó. `side` phải KHỚP loại đối
    tác (phải thu ↔ khách hàng, phải trả ↔ NCC): cổng quyền kiểm theo chiều,
    nên một cặp lệch chiều là đường vòng qua chính cổng đó — provider đã khóa
    chiều ở tầng dữ liệu (sửa H-1 review 6B), đây là lớp chặn sớm có thông điệp.
    """
    authorized.access.require(RECEIPT_VIEW if side == "receivable" else PAYMENT_VIEW)
    expected_kind = PartnerKind.CUSTOMER if side == "receivable" else PartnerKind.VENDOR
    if partner_kind != expected_kind.value:
        raise SettlementSideMismatchError(
            "Chiều công nợ không khớp loại đối tác — phải thu đi với khách hàng, "
            "phải trả đi với nhà cung cấp",
            side=side,
            partner_kind=partner_kind,
        )
    _require_branch_in_scope(authorized, branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        invoices = open_invoices(
            session,
            side=side,
            partner_kind=PartnerKind(partner_kind),
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )
        return OpenInvoicesResponse(
            items=tuple(OpenInvoiceOut.from_invoice(invoice) for invoice in invoices)
        )


@router.get("/vouchers/{voucher_id}", response_model=CashVoucherOut)
def get_cash_voucher(
    voucher_id: UUID, authorized: Authorized, factory: SessionFactory
) -> CashVoucherOut:
    with unit_of_work(factory, authorized.scope) as session:
        header, body, lines, settlements = CashVoucherService(session).get(voucher_id)
        _, view_permission = _kind_permissions(body.kind)
        authorized.access.require(view_permission)
        return _to_response(header, body, lines, settlements)


@router.put("/vouchers/{voucher_id}", response_model=CashVoucherOut)
def update_cash_voucher(
    voucher_id: UUID,
    payload: CashVoucherUpdate,
    authorized: Authorized,
    factory: SessionFactory,
) -> CashVoucherOut:
    """Sửa phiếu Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005)."""
    _require_branch_in_scope(authorized, payload.branch_id)
    authorized.access.require(
        permission_code(
            CASH_PERMISSION_MODULE,
            RECEIPT_PERMISSION_CODE
            if payload.kind == CashVoucherKind.RECEIPT
            else PAYMENT_PERMISSION_CODE,
            Action.EDIT,
        )
    )
    with unit_of_work(factory, authorized.scope) as session:
        service = CashVoucherService(session)
        voucher = service.update(
            voucher_id,
            payload,
            expected_row_version=payload.row_version,
            user_id=authorized.scope.user_id,
        )
        header, body, lines, settlements = service.get(voucher.id)
        return _to_response(header, body, lines, settlements)


def _sheet_response(service: CashCountSheetService, sheet_id: UUID) -> CountSheetOut:
    sheet = service.require(sheet_id)
    base = CountSheetOut.model_validate(sheet)
    return base.model_copy(
        update={
            "difference": sheet.counted_total - sheet.book_balance,
            "lines": tuple(
                CountSheetLineOut.model_validate(line) for line in service.lines_of(sheet_id)
            ),
        }
    )


@router.post("/count-sheets", response_model=CountSheetOut, status_code=status.HTTP_201_CREATED)
def create_count_sheet(
    payload: CountSheetIn,
    authorized: CountSheetAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CountSheetKey,
    response: Response,
) -> CountSheetOut:
    """Lập biên bản kiểm kê quỹ (FR-QUY-030) — chụp số sổ tại ngày kiểm kê."""
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[CountSheetOut, IdempotentRef]:
        service = CashCountSheetService(session)
        sheet = service.create(payload, user_id=authorized.scope.user_id)
        return _sheet_response(service, sheet.id), IdempotentRef(
            result_type="cash_count_sheets", result_id=str(sheet.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> CountSheetOut:
        return _sheet_response(CashCountSheetService(session), UUID(ref.result_id))

    created_body, created = execute_once(
        factory,
        authorized.scope,
        route_key=COUNT_SHEET_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_body


@router.get("/count-sheets", response_model=CountSheetListResponse)
def list_count_sheets(
    authorized: CountSheetReader,
    factory: SessionFactory,
    cash_account_id: Annotated[int | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> CountSheetListResponse:
    with unit_of_work(factory, authorized.scope) as session:
        service = CashCountSheetService(session)
        rows, total = service.list_page(
            cash_account_id=cash_account_id,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=page_size,
        )
        return CountSheetListResponse(
            items=tuple(_sheet_response(service, sheet.id) for sheet in rows),
            total=total,
            page=page,
            page_size=page_size,
        )


@router.get("/count-sheets/{sheet_id}", response_model=CountSheetOut)
def get_count_sheet(
    sheet_id: UUID, authorized: CountSheetReader, factory: SessionFactory
) -> CountSheetOut:
    with unit_of_work(factory, authorized.scope) as session:
        return _sheet_response(CashCountSheetService(session), sheet_id)


@router.delete("/count-sheets/{sheet_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_count_sheet(
    sheet_id: UUID, authorized: CountSheetRemover, factory: SessionFactory
) -> None:
    with unit_of_work(factory, authorized.scope) as session:
        CashCountSheetService(session).delete(sheet_id)


@router.post(
    "/count-sheets/{sheet_id}/actions/create-adjustment",
    response_model=CountSheetOut,
)
def create_count_sheet_adjustment(
    sheet_id: UUID,
    authorized: CountSheetAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AdjustmentKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> CountSheetOut:
    """Sinh phiếu thu/chi xử lý chênh lệch kiểm kê (FR-QUY-031).

    Người gọi phải có thêm quyền tạo đúng loại phiếu sẽ sinh (thừa → phiếu thu,
    thiếu → phiếu chi) — kiểm trong transaction vì phải đọc biên bản mới biết
    chiều chênh lệch.
    """

    def work(session: Session) -> tuple[CountSheetOut, IdempotentRef]:
        service = CashCountSheetService(session)
        sheet = service.require(sheet_id)
        surplus = sheet.counted_total - sheet.book_balance > 0
        authorized.access.require(RECEIPT_CREATE if surplus else PAYMENT_CREATE)
        service.create_adjustment(
            sheet_id,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledge_warnings,
        )
        return _sheet_response(service, sheet_id), IdempotentRef(
            result_type="cash_count_sheets", result_id=str(sheet_id)
        )

    def replay(session: Session, ref: IdempotentRef) -> CountSheetOut:
        return _sheet_response(CashCountSheetService(session), UUID(ref.result_id))

    result, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=ADJUSTMENT_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{ADJUSTMENT_ROUTE}:{sheet_id}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_200_OK
    return result
