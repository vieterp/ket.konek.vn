"""Endpoint hóa đơn bán hàng (`/api/v1/sales/*`) — SRS 06 §3, lát 7C-2.

Tạo/sửa/đọc thân hóa đơn + picker công nợ phải thu (cho chứng từ trả lại /
giảm giá đối trừ hóa đơn gốc) đi qua router của module (màn hình đọc một
module — không BFF, RT-21). Ghi sổ / bỏ ghi sổ / xóa dùng endpoint chứng từ
dùng chung (`routers/vouchers.py`): `SAL` đã đăng ký loại + hook vòng đời vào
registry của posting nên bên đó tự biết kiểm quyền nào, dựng định khoản ra sao
và ghi/gỡ sổ phụ công nợ khi nào. Vì thế router này KHÔNG có
`/actions/post|unpost` riêng như phác thảo trong plan phase 7 — hai đường ghi
sổ cho một loại chứng từ là hai chỗ để chúng lệch nhau, cùng lựa chọn với 7B.

`pending-issues` (BFF tab "việc còn thiếu", U1) thuộc lát 7G cùng với chiều mua.
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

# Picker hóa đơn còn nợ dùng chung hình dạng với phiếu thu/chi và hóa đơn mua:
# một tên schema trên OpenAPI, client sinh một type. Tầng API được phép nhìn
# cả hai module — luật C3 chỉ cấm module nhìn nhau.
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.modules.sales import INVOICE_PERMISSION_CODE, SALES_PERMISSION_MODULE
from ket.modules.sales.models import SalesInvoice, SalesInvoiceLine, SalesSettlement
from ket.modules.sales.schemas import (
    SalesInvoiceIn,
    SalesInvoiceLineOut,
    SalesInvoiceOut,
    SalesInvoiceUpdate,
    SalesSettlementOut,
)
from ket.modules.sales.service import SalesInvoiceService
from ket.modules.sales.settlement_service import open_invoices
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/sales", tags=["sales"])

INVOICE_CREATE = permission_code(SALES_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.CREATE)
INVOICE_VIEW = permission_code(SALES_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.VIEW)
INVOICE_EDIT = permission_code(SALES_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.EDIT)
INVOICE_POST = permission_code(SALES_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.POST)

InvoiceAuthor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_CREATE))]
InvoiceReader = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_VIEW))]
InvoiceEditor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_EDIT))]

CREATE_ROUTE: Final[str] = "POST /api/v1/sales/invoices"
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]

_RECEIVABLE_SIDE: Final[str] = "receivable"


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _to_response(
    voucher: Voucher,
    body: SalesInvoice,
    lines: list[SalesInvoiceLine],
    settlements: list[SalesSettlement],
) -> SalesInvoiceOut:
    base = SalesInvoiceOut.model_validate(voucher)
    return base.model_copy(
        update={
            "kind": body.kind,
            "operation_code": body.operation_code,
            "customer_id": body.customer_id,
            "salesperson_id": body.salesperson_id,
            "ship_to": body.ship_to,
            "recipient_name": body.recipient_name,
            "invoice_form": body.invoice_form,
            "invoice_serial": body.invoice_serial,
            "invoice_no": body.invoice_no,
            "invoice_date": body.invoice_date,
            "payment_term_id": body.payment_term_id,
            "due_date": body.due_date,
            "receivable_account_id": body.receivable_account_id,
            "price_list_id": body.price_list_id,
            "is_stock_issue": body.is_stock_issue,
            "cogs_posted": body.cogs_posted,
            "total_before_tax_fc": body.total_before_tax_fc,
            "total_discount_fc": body.total_discount_fc,
            "total_vat_fc": body.total_vat_fc,
            "total_fc": body.total_fc,
            "lines": tuple(SalesInvoiceLineOut.model_validate(line) for line in lines),
            "settlements": tuple(SalesSettlementOut.model_validate(row) for row in settlements),
        }
    )


def _response_of(service: SalesInvoiceService, voucher_id: UUID) -> SalesInvoiceOut:
    return _to_response(*service.get(voucher_id))


@router.post("/invoices", response_model=SalesInvoiceOut, status_code=status.HTTP_201_CREATED)
def create_sales_invoice(
    payload: SalesInvoiceIn,
    authorized: InvoiceAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> SalesInvoiceOut:
    """Cất hóa đơn bán; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction.

    `acknowledge_warnings` chỉ có tác dụng trên lượt ghi sổ đi kèm đó (FR-SYS-062
    mức "Cảnh báo" — ví dụ khách hàng vượt ngưỡng nợ, FR-SAL-034); mức "Chặn"
    không mở được.
    """
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[SalesInvoiceOut, IdempotentRef]:
        service = SalesInvoiceService(session)
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

    def replay(session: Session, ref: IdempotentRef) -> SalesInvoiceOut:
        return _response_of(SalesInvoiceService(session), UUID(ref.result_id))

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
def list_open_receivables(
    authorized: InvoiceReader,
    factory: SessionFactory,
    customer_id: Annotated[int, Query()],
    branch_id: Annotated[int, Query()],
    as_of: Annotated[date, Query()],
) -> OpenInvoicesResponse:
    """Hóa đơn bán còn nợ của một khách hàng — picker cho chứng từ trả lại /
    giảm giá hàng bán.

    Chỉ một chiều (phải thu) và chỉ loại đối tác khách hàng, khóa cứng thay vì
    nhận tham số: màn hình này chỉ tồn tại để chọn hóa đơn gốc cho một chứng từ
    bán. Hóa đơn **đã thu đủ** không nằm trong danh sách — và đó chính là lý do
    chứng từ giảm trừ cho một hóa đơn đã thu đủ không lập được: đường đúng lúc
    ấy là trả tiền lại khách bằng phiếu chi.
    """
    _require_branch_in_scope(authorized, branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        invoices = open_invoices(
            session,
            side=_RECEIVABLE_SIDE,
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=customer_id,
            branch_id=branch_id,
            as_of=as_of,
        )
        return OpenInvoicesResponse(
            items=tuple(OpenInvoiceOut.from_invoice(invoice) for invoice in invoices)
        )


@router.get("/invoices/{voucher_id}", response_model=SalesInvoiceOut)
def get_sales_invoice(
    voucher_id: UUID, authorized: InvoiceReader, factory: SessionFactory
) -> SalesInvoiceOut:
    with unit_of_work(factory, authorized.scope) as session:
        return _response_of(SalesInvoiceService(session), voucher_id)


@router.put("/invoices/{voucher_id}", response_model=SalesInvoiceOut)
def update_sales_invoice(
    voucher_id: UUID,
    payload: SalesInvoiceUpdate,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> SalesInvoiceOut:
    """Sửa hóa đơn Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005)."""
    _require_branch_in_scope(authorized, payload.branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        service = SalesInvoiceService(session)
        voucher = service.update(
            voucher_id,
            payload,
            expected_row_version=payload.row_version,
            user_id=authorized.scope.user_id,
        )
        return _response_of(service, voucher.id)
