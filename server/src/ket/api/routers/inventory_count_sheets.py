"""Endpoint kiểm kê kho (`/api/v1/inventory/count-sheets/*`) — U8, FR-STK-030/031,
lát 8D; khuôn kiểm kê quỹ 6E.

Luồng U8 là bốn lượt gọi: lập (chụp số sổ) → nhập số đếm → đọc **chỉ** chênh
lệch → duyệt (tự sinh phiếu xử lý). Không lượt nào bắt người dùng tự lập chứng
từ điều chỉnh — đó là cả điểm của U8.

Router riêng tệp (không nhét vào `routers/inventory.py`): biên bản **không phải
chứng từ** — không có dòng `vouchers`, không ghi sổ, không có `document_type`
trong registry của posting; gộp chung sẽ làm hai vòng đời rất khác nhau nằm
cạnh nhau trong một tệp 500 dòng.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.inventory_count_sheets_schemas import (
    InventoryCountSheetListResponse,
    InventoryCountSheetListRow,
)
from ket.kernel.errors import BranchNotInScopeError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.inventory import COUNT_SHEET_PERMISSION_CODE, INVENTORY_PERMISSION_MODULE
from ket.modules.inventory.count_sheet_service import InventoryCountSheetService
from ket.modules.inventory.schemas import (
    InventoryCountSheetAdjustmentResponse,
    InventoryCountSheetCountsIn,
    InventoryCountSheetDifferencesResponse,
    InventoryCountSheetIn,
    InventoryCountSheetLineOut,
    InventoryCountSheetOut,
)

router = APIRouter(prefix="/api/v1/inventory/count-sheets", tags=["inventory"])

MAX_PAGE_SIZE: Final[int] = 200


def _permission(action: Action) -> str:
    return permission_code(INVENTORY_PERMISSION_MODULE, COUNT_SHEET_PERMISSION_CODE, action)


SheetReader = Annotated[AuthorizedRequest, Depends(require_permission(_permission(Action.VIEW)))]
SheetAuthor = Annotated[AuthorizedRequest, Depends(require_permission(_permission(Action.CREATE)))]
SheetEditor = Annotated[AuthorizedRequest, Depends(require_permission(_permission(Action.EDIT)))]
SheetRemover = Annotated[AuthorizedRequest, Depends(require_permission(_permission(Action.DELETE)))]
SheetApprover = Annotated[AuthorizedRequest, Depends(require_permission(_permission(Action.POST)))]

CREATE_ROUTE: Final[str] = "POST /api/v1/inventory/count-sheets"
APPLY_ROUTE: Final[str] = "POST /api/v1/inventory/count-sheets/{sheet_id}/actions/apply-differences"
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]
ApplyKey = Annotated[str, Depends(idempotency_key_dependency(APPLY_ROUTE))]


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh không nằm trong phạm vi của người dùng", branch=branch_id
        )


def _sheet_response(service: InventoryCountSheetService, sheet_id: UUID) -> InventoryCountSheetOut:
    sheet = service.require(sheet_id)
    return InventoryCountSheetOut.model_validate(sheet).model_copy(
        update={
            "lines": tuple(
                InventoryCountSheetLineOut.model_validate(line)
                for line in service.lines_of(sheet_id)
            )
        }
    )


@router.post("", response_model=InventoryCountSheetOut, status_code=status.HTTP_201_CREATED)
def create_count_sheet(
    payload: InventoryCountSheetIn,
    authorized: SheetAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
) -> InventoryCountSheetOut:
    """Lập biên bản kiểm kê kho (FR-STK-030) — chụp tồn sổ sách tại ngày kiểm kê."""
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[InventoryCountSheetOut, IdempotentRef]:
        service = InventoryCountSheetService(session)
        sheet = service.create(payload, user_id=authorized.scope.user_id)
        return _sheet_response(service, sheet.id), IdempotentRef(
            result_type="inventory_count_sheets", result_id=str(sheet.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> InventoryCountSheetOut:
        return _sheet_response(InventoryCountSheetService(session), UUID(ref.result_id))

    body, created = execute_once(
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
    return body


@router.get("", response_model=InventoryCountSheetListResponse)
def list_count_sheets(
    authorized: SheetReader,
    factory: SessionFactory,
    warehouse_id: Annotated[int | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> InventoryCountSheetListResponse:
    with unit_of_work(factory, authorized.scope) as session:
        service = InventoryCountSheetService(session)
        rows, total = service.list_page(
            warehouse_id=warehouse_id,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=page_size,
        )
        return InventoryCountSheetListResponse(
            # Một câu cho cả trang: `_sheet_response` là hai câu MỖI biên bản
            # (thân + dòng), và danh sách không cần dòng nào.
            items=tuple(InventoryCountSheetListRow.model_validate(sheet) for sheet in rows),
            total=total,
            page=page,
            page_size=page_size,
        )


@router.get("/{sheet_id}", response_model=InventoryCountSheetOut)
def get_count_sheet(
    sheet_id: UUID, authorized: SheetReader, factory: SessionFactory
) -> InventoryCountSheetOut:
    with unit_of_work(factory, authorized.scope) as session:
        return _sheet_response(InventoryCountSheetService(session), sheet_id)


@router.put("/{sheet_id}/counts", response_model=InventoryCountSheetOut)
def set_counts(
    sheet_id: UUID,
    payload: InventoryCountSheetCountsIn,
    authorized: SheetEditor,
    factory: SessionFactory,
) -> InventoryCountSheetOut:
    """Nhập / sửa số đếm thật (FR-STK-030) — sửa được tới khi duyệt."""
    with unit_of_work(factory, authorized.scope) as session:
        service = InventoryCountSheetService(session)
        service.set_counts(sheet_id, payload)
        return _sheet_response(service, sheet_id)


@router.get("/{sheet_id}/differences", response_model=InventoryCountSheetDifferencesResponse)
def read_differences(
    sheet_id: UUID, authorized: SheetReader, factory: SessionFactory
) -> InventoryCountSheetDifferencesResponse:
    """U8: **chỉ** dòng đã đếm và lệch — dòng khớp không hiện."""
    with unit_of_work(factory, authorized.scope) as session:
        service = InventoryCountSheetService(session)
        sheet = service.require(sheet_id)
        return InventoryCountSheetDifferencesResponse(
            sheet_id=sheet.id,
            count_date=sheet.count_date,
            differences=tuple(service.differences(sheet_id)),
        )


@router.delete("/{sheet_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_count_sheet(sheet_id: UUID, authorized: SheetRemover, factory: SessionFactory) -> None:
    with unit_of_work(factory, authorized.scope) as session:
        InventoryCountSheetService(session).delete(sheet_id)


@router.post(
    "/{sheet_id}/actions/apply-differences", response_model=InventoryCountSheetAdjustmentResponse
)
def apply_differences(
    sheet_id: UUID,
    authorized: SheetApprover,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: ApplyKey,
    acknowledged_warnings: Annotated[bool, Query()] = False,
) -> InventoryCountSheetAdjustmentResponse:
    """Duyệt biên bản (FR-STK-031): tự sinh phiếu nhập (thừa) / phiếu xuất
    (thiếu) đã định khoản sẵn, ở trạng thái **nháp** — kế toán xem rồi ghi sổ."""

    def work(session: Session) -> tuple[InventoryCountSheetAdjustmentResponse, IdempotentRef]:
        receipt, issue = InventoryCountSheetService(session).apply_differences(
            sheet_id,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        return InventoryCountSheetAdjustmentResponse(
            sheet_id=sheet_id,
            receipt_voucher_id=receipt.id if receipt is not None else None,
            issue_voucher_id=issue.id if issue is not None else None,
        ), IdempotentRef(result_type="inventory_count_sheets", result_id=str(sheet_id))

    def replay(session: Session, ref: IdempotentRef) -> InventoryCountSheetAdjustmentResponse:
        sheet = InventoryCountSheetService(session).require(UUID(ref.result_id))
        return InventoryCountSheetAdjustmentResponse(
            sheet_id=sheet.id,
            receipt_voucher_id=sheet.adjustment_receipt_id,
            issue_voucher_id=sheet.adjustment_issue_id,
        )

    body, _created = execute_once(
        factory,
        authorized.scope,
        route_key=APPLY_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(str(sheet_id)),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    return body
