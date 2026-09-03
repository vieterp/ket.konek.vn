"""Endpoint hóa đơn mua hàng (`/api/v1/purchase/*`) — SRS 05 §3, lát 7B.

Tạo/sửa/đọc thân hóa đơn + picker công nợ phải trả (cho chứng từ trả lại hàng
đối trừ hóa đơn gốc) đi qua router của module (màn hình đọc một module — không
BFF, RT-21). Ghi sổ / bỏ ghi sổ / xóa dùng endpoint chứng từ dùng chung
(`routers/vouchers.py`): `PUR` đã đăng ký loại + hook vòng đời vào registry của
posting nên bên đó tự biết kiểm quyền nào, dựng định khoản ra sao và ghi/gỡ sổ
phụ công nợ khi nào. Vì thế router này KHÔNG có `/actions/post|unpost` riêng
như phác thảo trong plan phase 7 — hai đường ghi sổ cho một loại chứng từ là
hai chỗ để chúng lệch nhau.
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
from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import BranchNotInScopeError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code

# Picker hóa đơn còn nợ dùng chung hình dạng với phiếu thu/chi (`bank.py` cũng
# mượn): một tên schema trên OpenAPI, client sinh một type. Tầng API được phép
# nhìn cả hai module — luật C3 chỉ cấm module nhìn nhau.
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.modules.purchase import INVOICE_PERMISSION_CODE, PURCHASE_PERMISSION_MODULE
from ket.modules.purchase.models import (
    LandedCost,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseSettlement,
)
from ket.modules.purchase.schemas import (
    LandedCostOut,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineOut,
    PurchaseInvoiceOut,
    PurchaseInvoiceUpdate,
    PurchaseSettlementOut,
)
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.purchase.settlement_service import open_invoices
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/purchase", tags=["purchase"])

INVOICE_CREATE = permission_code(PURCHASE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.CREATE)
INVOICE_VIEW = permission_code(PURCHASE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.VIEW)
INVOICE_EDIT = permission_code(PURCHASE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.EDIT)
INVOICE_POST = permission_code(PURCHASE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.POST)

InvoiceAuthor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_CREATE))]
InvoiceReader = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_VIEW))]
InvoiceEditor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_EDIT))]

CREATE_ROUTE: Final[str] = "POST /api/v1/purchase/invoices"
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]

_PAYABLE_SIDE: Final[str] = "payable"


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _to_response(
    voucher: Voucher,
    body: PurchaseInvoice,
    lines: list[PurchaseInvoiceLine],
    costs: list[LandedCost],
    settlements: list[PurchaseSettlement],
) -> PurchaseInvoiceOut:
    base = PurchaseInvoiceOut.model_validate(voucher)
    return base.model_copy(
        update={
            "kind": body.kind,
            "operation_code": body.operation_code,
            "vendor_id": body.vendor_id,
            "vendor_invoice_status": body.vendor_invoice_status,
            "vendor_invoice_form": body.vendor_invoice_form,
            "vendor_invoice_serial": body.vendor_invoice_serial,
            "vendor_invoice_no": body.vendor_invoice_no,
            "vendor_invoice_date": body.vendor_invoice_date,
            "payment_term_id": body.payment_term_id,
            "due_date": body.due_date,
            "payable_account_id": body.payable_account_id,
            "landed_cost_allocation": body.landed_cost_allocation,
            "total_before_tax_fc": body.total_before_tax_fc,
            "total_vat_fc": body.total_vat_fc,
            "total_landed_cost_fc": body.total_landed_cost_fc,
            "total_fc": body.total_fc,
            "lines": tuple(PurchaseInvoiceLineOut.model_validate(line) for line in lines),
            "landed_costs": tuple(LandedCostOut.model_validate(cost) for cost in costs),
            "settlements": tuple(PurchaseSettlementOut.model_validate(row) for row in settlements),
        }
    )


def _response_of(service: PurchaseInvoiceService, voucher_id: UUID) -> PurchaseInvoiceOut:
    return _to_response(*service.get(voucher_id))


@router.post("/invoices", response_model=PurchaseInvoiceOut, status_code=status.HTTP_201_CREATED)
def create_purchase_invoice(
    payload: PurchaseInvoiceIn,
    authorized: InvoiceAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> PurchaseInvoiceOut:
    """Cất hóa đơn mua; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction.

    `acknowledge_warnings` chỉ có tác dụng trên lượt ghi sổ đi kèm đó (FR-SYS-062
    mức "Cảnh báo" — ví dụ nhà cung cấp vượt ngưỡng nợ); mức "Chặn" không mở được.
    """
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[PurchaseInvoiceOut, IdempotentRef]:
        service = PurchaseInvoiceService(session)
        if value_of(session, key=SAVE_ALSO_POSTS_KEY, user_id=authorized.scope.user_id) is True:
            authorized.access.require(INVOICE_POST)
        voucher = service.create(
            payload,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledge_warnings,
        )
        return _response_of(service, voucher.id), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> PurchaseInvoiceOut:
        return _response_of(PurchaseInvoiceService(session), UUID(ref.result_id))

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
def list_open_payables(
    authorized: InvoiceReader,
    factory: SessionFactory,
    vendor_id: Annotated[int, Query()],
    branch_id: Annotated[int, Query()],
    as_of: Annotated[date, Query()],
) -> OpenInvoicesResponse:
    """Hóa đơn mua còn nợ của một nhà cung cấp — picker cho chứng từ trả lại hàng.

    Chỉ một chiều (phải trả) và chỉ loại đối tác NCC, khóa cứng thay vì nhận
    tham số: màn hình này chỉ tồn tại để chọn hóa đơn gốc cho một chứng từ mua.
    """
    _require_branch_in_scope(authorized, branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        invoices = open_invoices(
            session,
            side=_PAYABLE_SIDE,
            partner_kind=PartnerKind.VENDOR,
            partner_id=vendor_id,
            branch_id=branch_id,
            as_of=as_of,
        )
        return OpenInvoicesResponse(
            items=tuple(OpenInvoiceOut.from_invoice(invoice) for invoice in invoices)
        )


@router.get("/invoices/{voucher_id}", response_model=PurchaseInvoiceOut)
def get_purchase_invoice(
    voucher_id: UUID, authorized: InvoiceReader, factory: SessionFactory
) -> PurchaseInvoiceOut:
    with unit_of_work(factory, authorized.scope) as session:
        return _response_of(PurchaseInvoiceService(session), voucher_id)


@router.put("/invoices/{voucher_id}", response_model=PurchaseInvoiceOut)
def update_purchase_invoice(
    voucher_id: UUID,
    payload: PurchaseInvoiceUpdate,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> PurchaseInvoiceOut:
    """Sửa hóa đơn Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005)."""
    _require_branch_in_scope(authorized, payload.branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        service = PurchaseInvoiceService(session)
        voucher = service.update(
            voucher_id,
            payload,
            expected_row_version=payload.row_version,
            user_id=authorized.scope.user_id,
        )
        return _response_of(service, voucher.id)
