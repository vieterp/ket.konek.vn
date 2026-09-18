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

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import Select, and_, case, func, select
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
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code

# Picker hóa đơn còn nợ dùng chung hình dạng với phiếu thu/chi (`bank.py` cũng
# mượn): một tên schema trên OpenAPI, client sinh một type. Tầng API được phép
# nhìn cả hai module — luật C3 chỉ cấm module nhìn nhau.
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.modules.purchase import INVOICE_PERMISSION_CODE, PURCHASE_PERMISSION_MODULE
from ket.modules.purchase.models import (
    LandedCost,
    PurchaseInvoice,
    PurchaseInvoiceKind,
    PurchaseInvoiceLine,
    PurchaseSettlement,
)
from ket.modules.purchase.schemas import (
    LandedCostOut,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineOut,
    PurchaseInvoiceListItem,
    PurchaseInvoiceListResponse,
    PurchaseInvoiceListTotals,
    PurchaseInvoiceOut,
    PurchaseInvoiceUpdate,
    PurchaseSettlementOut,
)
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.purchase.settlement_service import open_invoices

# Lưới chứng từ đọc "còn phải trả hiện nay" từ dòng sổ phụ của chính chứng từ.
# Tầng api nhìn được cả `purchase` lẫn `receivables` (C3 chỉ cấm module nhìn
# nhau); `settled` là scalar CHẠY nên đúng con số "hiện nay" mà lưới hỏi — khác
# báo cáo theo mốc chốt của 7G-1, nơi phải cắt lượt trả theo ngày ghi sổ.
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.documents.models import Voucher, VoucherStatus

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

LIST_MAX_PAGE_SIZE: Final[int] = 200
_ZERO: Final[Decimal] = Decimal(0)

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
            "buyer_id": body.buyer_id,
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


def _invoice_list_query(
    *,
    as_of: date,
    period_id: int | None,
    voucher_status: int | None,
    vendor_id: int | None,
    kind: int | None,
    vendor_invoice_status: int | None,
    overdue: bool,
    from_date: date | None,
    to_date: date | None,
) -> Select[tuple[Voucher, PurchaseInvoice, str, str, Decimal, date | None]]:
    """Một câu SELECT cho cả trang lẫn dòng tổng — hai phép đọc không được lệch bộ lọc.

    Kiểu suy ra của SQLAlchemy không biết `outerjoin` làm ba cột NCC/còn nợ
    thành nullable — `_list_item` nhận `| None` và xử tại chỗ.
    """
    remaining = (ArApLedgerEntry.amount_fc - ArApLedgerEntry.settled_fc).label("remaining_fc")
    query = (
        select(
            Voucher,
            PurchaseInvoice,
            Partner.code,
            Partner.name,
            remaining,
            ArApLedgerEntry.due_date,
        )
        .join(PurchaseInvoice, PurchaseInvoice.id == Voucher.id)
        .outerjoin(Partner, Partner.id == PurchaseInvoice.vendor_id)
        # Một hóa đơn có thể mang NHIỀU dòng sổ phụ: mỗi khoản chi phí mua do
        # NCC khác thu là một khoản phải trả riêng cùng `document_id`
        # (`PurchaseInvoiceService._subledger_entries`). Lưới nói về công nợ
        # với NCC TRÊN hóa đơn, nên ghim đúng dòng (NCC, TK phải trả) của hóa
        # đơn — không thì mỗi khoản chi phí nhân đôi dòng lưới và dòng tổng.
        # Nợ với NCC vận chuyển sống ở báo cáo tuổi nợ / thẻ đối tác.
        .outerjoin(
            ArApLedgerEntry,
            and_(
                ArApLedgerEntry.document_id == Voucher.id,
                ArApLedgerEntry.partner_id == PurchaseInvoice.vendor_id,
                ArApLedgerEntry.account_id == PurchaseInvoice.payable_account_id,
                ArApLedgerEntry.target_kind == SettlementTargetKind.PURCHASE_INVOICE.value,
                ArApLedgerEntry.partner_kind == PartnerKind.VENDOR.value,
                ArApLedgerEntry.ledger == 0,
            ),
        )
    )
    if period_id is not None:
        query = query.where(Voucher.period_id == period_id)
    if voucher_status is not None:
        query = query.where(Voucher.status == voucher_status)
    if vendor_id is not None:
        query = query.where(PurchaseInvoice.vendor_id == vendor_id)
    if kind is not None:
        query = query.where(PurchaseInvoice.kind == kind)
    if vendor_invoice_status is not None:
        query = query.where(PurchaseInvoice.vendor_invoice_status == vendor_invoice_status)
    if from_date is not None:
        query = query.where(Voucher.posting_date >= from_date)
    if to_date is not None:
        query = query.where(Voucher.posting_date <= to_date)
    if overdue:
        # "Quá hạn" = còn nợ và hạn đã qua tại `as_of` — cùng định nghĩa với nhóm
        # `qua-han` của BFF `pending-issues`, nên bấm tab ấy rồi lọc lưới ra
        # đúng những chứng từ tab vừa đếm.
        query = query.where(
            ArApLedgerEntry.due_date < as_of,
            ArApLedgerEntry.amount_fc > ArApLedgerEntry.settled_fc,
        )
    return query


@router.get("/invoices", response_model=PurchaseInvoiceListResponse)
def list_purchase_invoices(
    authorized: InvoiceReader,
    factory: SessionFactory,
    period_id: Annotated[int | None, Query()] = None,
    voucher_status: Annotated[int | None, Query(alias="status")] = None,
    vendor_id: Annotated[int | None, Query()] = None,
    kind: Annotated[int | None, Query()] = None,
    vendor_invoice_status: Annotated[int | None, Query()] = None,
    overdue: Annotated[bool, Query()] = False,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    as_of: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=LIST_MAX_PAGE_SIZE)] = 50,
) -> PurchaseInvoiceListResponse:
    """Lưới chứng từ mua hàng (màn 01 design): mới nhất trước, kèm dòng tổng.

    Màn hình đọc một module nên gọi router module chứ không BFF (RT-21); lưới
    chứng từ dùng chung `/vouchers` không mang NCC, hóa đơn NCC hay số còn nợ
    — ba cột mà tab "việc còn thiếu" cần để nói ra việc tiếp theo. RLS lọc
    chi nhánh trước khi mã này chạy.
    """
    effective_as_of = as_of if as_of is not None else datetime.now(UTC).astimezone().date()
    query = _invoice_list_query(
        as_of=effective_as_of,
        period_id=period_id,
        voucher_status=voucher_status,
        vendor_id=vendor_id,
        kind=kind,
        vendor_invoice_status=vendor_invoice_status,
        overdue=overdue,
        from_date=from_date,
        to_date=to_date,
    )
    with unit_of_work(factory, authorized.scope) as session:
        filtered = query.subquery()
        # Tờ trả lại hàng trừ vào tổng mua (giá trị mua ròng); nhóm theo tiền tệ
        # vì nguyên tệ khác nhau không cộng được (user chốt 2026-09-18).
        signed_total = case(
            (filtered.c.kind == PurchaseInvoiceKind.RETURN, -filtered.c.total_fc),
            else_=filtered.c.total_fc,
        )
        totals_rows = session.execute(
            select(
                filtered.c.currency_code,
                func.count(),
                func.coalesce(func.sum(signed_total), _ZERO),
                func.coalesce(func.sum(filtered.c.remaining_fc), _ZERO),
            )
            .group_by(filtered.c.currency_code)
            .order_by(filtered.c.currency_code)
        ).all()
        rows = session.execute(
            query.order_by(Voucher.posting_date.desc(), Voucher.voucher_no.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = tuple(
            _list_item(
                voucher, body, vendor_code, vendor_name, remaining, due_date, effective_as_of
            )
            for voucher, body, vendor_code, vendor_name, remaining, due_date in rows
        )
        totals = tuple(
            PurchaseInvoiceListTotals(
                currency_code=str(currency),
                count=int(count),
                total_fc=Decimal(total_fc),
                remaining_fc=Decimal(remaining_fc),
            )
            for currency, count, total_fc, remaining_fc in totals_rows
        )
        return PurchaseInvoiceListResponse(
            items=items,
            total=sum(row.count for row in totals),
            totals=totals,
            page=page,
            page_size=page_size,
            as_of=effective_as_of,
        )


def _list_item(
    voucher: Voucher,
    body: PurchaseInvoice,
    vendor_code: str | None,
    vendor_name: str | None,
    remaining: Decimal | None,
    ledger_due_date: date | None,
    as_of: date,
) -> PurchaseInvoiceListItem:
    # Hạn trả trên dòng sổ phụ là hạn có hiệu lực (điều khoản NCC điền khi thân
    # không khai); chứng từ chưa ghi sổ chỉ có hạn khai trên thân.
    due_date = ledger_due_date if ledger_due_date is not None else body.due_date
    days_overdue: int | None = None
    if (
        remaining is not None
        and remaining > _ZERO
        and due_date is not None
        and due_date < as_of
        and voucher.status == VoucherStatus.DA_GHI_SO
    ):
        days_overdue = (as_of - due_date).days
    return PurchaseInvoiceListItem(
        id=voucher.id,
        voucher_no=voucher.voucher_no,
        branch_id=voucher.branch_id,
        document_date=voucher.document_date,
        posting_date=voucher.posting_date,
        status=voucher.status,
        currency_code=voucher.currency_code,
        kind=body.kind,
        vendor_id=body.vendor_id,
        vendor_code=vendor_code,
        vendor_name=vendor_name,
        vendor_invoice_status=body.vendor_invoice_status,
        vendor_invoice_form=body.vendor_invoice_form,
        vendor_invoice_serial=body.vendor_invoice_serial,
        vendor_invoice_no=body.vendor_invoice_no,
        total_fc=body.total_fc,
        remaining_fc=remaining,
        due_date=due_date,
        days_overdue=days_overdue,
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
