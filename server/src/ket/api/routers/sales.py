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

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import ScalarSelect, Select, and_, case, exists, func, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.expression import Exists

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
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code

# Picker hóa đơn còn nợ dùng chung hình dạng với phiếu thu/chi và hóa đơn mua:
# một tên schema trên OpenAPI, client sinh một type. Tầng API được phép nhìn
# cả hai module — luật C3 chỉ cấm module nhìn nhau.
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.modules.einvoice.models import LIVE_STATUSES, EInvoice

# Lưới chứng từ đọc "còn phải thu hiện nay" từ dòng sổ phụ của chính chứng từ
# và "có tờ HĐĐT còn sống" từ bảng hóa đơn điện tử. Tầng api nhìn được cả ba
# module (C3 chỉ cấm module nhìn nhau); `settled` là scalar CHẠY nên đúng con
# số "hiện nay" mà lưới hỏi — khác báo cáo theo mốc chốt của 7G-2b.
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales import INVOICE_PERMISSION_CODE, SALES_PERMISSION_MODULE
from ket.modules.sales.models import (
    KINDS_NEEDING_EINVOICE,
    REVERSING_KINDS,
    SalesInvoice,
    SalesInvoiceLine,
    SalesSettlement,
)
from ket.modules.sales.schemas import (
    SalesInvoiceIn,
    SalesInvoiceLineOut,
    SalesInvoiceListItem,
    SalesInvoiceListResponse,
    SalesInvoiceListTotals,
    SalesInvoiceOut,
    SalesInvoiceUpdate,
    SalesSettlementOut,
)
from ket.modules.sales.service import SalesInvoiceService
from ket.modules.sales.settlement_service import open_invoices
from ket.posting.documents.models import Voucher, VoucherStatus

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

LIST_MAX_PAGE_SIZE: Final[int] = 200
_ZERO: Final[Decimal] = Decimal(0)

EInvoiceFilter = Literal["missing"]
"""Bộ lọc `einvoice=` của lưới. Chỉ một giá trị ở lát này: `missing` — đã ghi
sổ, loại cần hóa đơn, không tờ còn sống — đúng điều kiện nhóm `chua-co-hoa-don`
của BFF `pending-issues`, để bấm tab ấy rồi lọc lưới ra đúng những chứng từ tab
vừa đếm. Các giá trị theo trạng thái CQT là việc của cột gộp U3 (7H-3)."""


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
            "adjusts_voucher_id": body.adjusts_voucher_id,
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


def _live_einvoice_exists() -> Exists:
    # Bí danh riêng, cùng lý do `_live_einvoice_id`: `EInvoice` đã đứng ở FROM
    # của câu ngoài.
    candidate = aliased(EInvoice)
    return exists().where(
        candidate.source_voucher_id == Voucher.id,
        candidate.status.in_(LIVE_STATUSES),
    )


def _live_einvoice_id() -> ScalarSelect[UUID]:
    """Id của MỘT tờ HĐĐT còn sống treo lên chứng từ, `NULL` khi không có.

    Subquery vô hướng chọn id rồi JOIN, chứ không JOIN thẳng theo trạng thái:
    chỉ mục riêng phần `uq_einvoices_live_source_voucher` cho một chứng từ MỘT
    tờ chưa xử lý xong (0–4), nhưng `DA_DIEU_CHINH` (6) vừa còn sống vừa nằm
    ngoài chỉ mục ấy — một chứng từ điều chỉnh hai lần mang HAI tờ 6 — nên JOIN
    theo trạng thái nhân đôi dòng lưới, và hai subquery độc lập (ký hiệu, số)
    có thể lấy hai tờ khác nhau. Thứ tự: trạng thái cao nhất (tờ đã đi xa nhất
    trong vòng đời), rồi tờ mới nhất — `id` là uuid7 nên giảm dần theo id là
    mới trước; khóa hòa để hai lượt gọi ra cùng một tờ.
    """
    # Bí danh riêng: `EInvoice` cũng đứng ở FROM của câu ngoài (JOIN theo id
    # này), nên không có bí danh thì SQLAlchemy tự tương quan cả bảng và
    # subquery mất FROM. Chỉ tương quan với `Voucher`.
    candidate = aliased(EInvoice)
    return (
        select(candidate.id)
        .where(
            candidate.source_voucher_id == Voucher.id,
            candidate.status.in_(LIVE_STATUSES),
        )
        .order_by(candidate.status.desc(), candidate.id.desc())
        .limit(1)
        .correlate(Voucher)
        .scalar_subquery()
    )


def _invoice_list_query(
    *,
    as_of: date,
    period_id: int | None,
    voucher_status: int | None,
    customer_id: int | None,
    kind: int | None,
    einvoice: EInvoiceFilter | None,
    overdue: bool,
    from_date: date | None,
    to_date: date | None,
) -> Select[tuple[Voucher, SalesInvoice, str, str, bool, str, str | None, Decimal, date | None]]:
    """Một câu SELECT cho cả trang lẫn dòng tổng — hai phép đọc không được lệch bộ lọc.

    Kiểu suy ra của SQLAlchemy không biết `outerjoin` làm các cột khách / ký
    hiệu hóa đơn / còn nợ thành nullable — `_list_item` nhận `| None` và xử tại chỗ.
    """
    remaining = (ArApLedgerEntry.amount_fc - ArApLedgerEntry.settled_fc).label("remaining_fc")
    has_live_einvoice = _live_einvoice_exists().label("has_live_einvoice")
    query = (
        select(
            Voucher,
            SalesInvoice,
            Partner.code,
            Partner.name,
            has_live_einvoice,
            InvoiceForm.code,
            EInvoice.invoice_no,
            remaining,
            ArApLedgerEntry.due_date,
        )
        .join(SalesInvoice, SalesInvoice.id == Voucher.id)
        .outerjoin(Partner, Partner.id == SalesInvoice.customer_id)
        .outerjoin(EInvoice, EInvoice.id == _live_einvoice_id())
        .outerjoin(InvoiceForm, InvoiceForm.id == EInvoice.invoice_form_id)
        # Hóa đơn bán chỉ có MỘT dòng sổ phụ (`_subledger_entries`: một khách,
        # một TK phải thu) — ghim đủ (khách, TK, loại đích, sổ) cùng khuôn với
        # lưới mua để hai chỗ đọc như nhau, không phải vì sợ nhân đôi.
        .outerjoin(
            ArApLedgerEntry,
            and_(
                ArApLedgerEntry.document_id == Voucher.id,
                ArApLedgerEntry.partner_id == SalesInvoice.customer_id,
                ArApLedgerEntry.account_id == SalesInvoice.receivable_account_id,
                ArApLedgerEntry.target_kind == SettlementTargetKind.SALES_INVOICE.value,
                ArApLedgerEntry.partner_kind == PartnerKind.CUSTOMER.value,
                ArApLedgerEntry.ledger == 0,
            ),
        )
    )
    if period_id is not None:
        query = query.where(Voucher.period_id == period_id)
    if voucher_status is not None:
        query = query.where(Voucher.status == voucher_status)
    if customer_id is not None:
        query = query.where(SalesInvoice.customer_id == customer_id)
    if kind is not None:
        query = query.where(SalesInvoice.kind == kind)
    if from_date is not None:
        query = query.where(Voucher.posting_date >= from_date)
    if to_date is not None:
        query = query.where(Voucher.posting_date <= to_date)
    if einvoice == "missing":
        query = query.where(
            Voucher.status == int(VoucherStatus.DA_GHI_SO),
            SalesInvoice.kind.in_(KINDS_NEEDING_EINVOICE),
            ~_live_einvoice_exists(),
        )
    if overdue:
        # "Quá hạn" = còn nợ và hạn đã qua tại `as_of` — cùng định nghĩa với nhóm
        # `qua-han` của BFF `pending-issues`.
        query = query.where(
            ArApLedgerEntry.due_date < as_of,
            ArApLedgerEntry.amount_fc > ArApLedgerEntry.settled_fc,
        )
    return query


@router.get("/invoices", response_model=SalesInvoiceListResponse)
def list_sales_invoices(
    authorized: InvoiceReader,
    factory: SessionFactory,
    period_id: Annotated[int | None, Query()] = None,
    voucher_status: Annotated[int | None, Query(alias="status")] = None,
    customer_id: Annotated[int | None, Query()] = None,
    kind: Annotated[int | None, Query()] = None,
    einvoice: Annotated[EInvoiceFilter | None, Query()] = None,
    overdue: Annotated[bool, Query()] = False,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    as_of: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=LIST_MAX_PAGE_SIZE)] = 50,
) -> SalesInvoiceListResponse:
    """Lưới chứng từ bán hàng (bộ xương màn 01 dùng lại): mới nhất trước, kèm dòng tổng.

    Màn hình đọc một module nên gọi router module chứ không BFF (RT-21); lưới
    chứng từ dùng chung `/vouchers` không mang khách hàng, hóa đơn hay số còn
    nợ — ba cột mà tab "việc còn thiếu" cần để nói ra việc tiếp theo. RLS lọc
    chi nhánh trước khi mã này chạy.
    """
    effective_as_of = as_of if as_of is not None else datetime.now(UTC).astimezone().date()
    query = _invoice_list_query(
        as_of=effective_as_of,
        period_id=period_id,
        voucher_status=voucher_status,
        customer_id=customer_id,
        kind=kind,
        einvoice=einvoice,
        overdue=overdue,
        from_date=from_date,
        to_date=to_date,
    )
    with unit_of_work(factory, authorized.scope) as session:
        filtered = query.subquery()
        # Ba loại giảm trừ trừ vào tổng bán (doanh thu ròng); nhóm theo tiền tệ
        # vì nguyên tệ khác nhau không cộng được (cùng luật 7H-1 M-2).
        signed_total = case(
            (filtered.c.kind.in_(REVERSING_KINDS), -filtered.c.total_fc),
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
                voucher,
                body,
                customer_code,
                customer_name,
                bool(has_live),
                einvoice_serial,
                einvoice_no,
                remaining,
                due_date,
                effective_as_of,
            )
            for (
                voucher,
                body,
                customer_code,
                customer_name,
                has_live,
                einvoice_serial,
                einvoice_no,
                remaining,
                due_date,
            ) in rows
        )
        totals = tuple(
            SalesInvoiceListTotals(
                currency_code=str(currency),
                count=int(count),
                total_fc=Decimal(total_fc),
                remaining_fc=Decimal(remaining_fc),
            )
            for currency, count, total_fc, remaining_fc in totals_rows
        )
        return SalesInvoiceListResponse(
            items=items,
            total=sum(row.count for row in totals),
            totals=totals,
            page=page,
            page_size=page_size,
            as_of=effective_as_of,
        )


def _list_item(
    voucher: Voucher,
    body: SalesInvoice,
    customer_code: str | None,
    customer_name: str | None,
    has_live_einvoice: bool,
    einvoice_serial: str | None,
    einvoice_no: str | None,
    remaining: Decimal | None,
    ledger_due_date: date | None,
    as_of: date,
) -> SalesInvoiceListItem:
    # Hạn thu trên dòng sổ phụ là hạn có hiệu lực (điều khoản khách điền khi
    # thân không khai); chứng từ chưa ghi sổ chỉ có hạn khai trên thân.
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
    return SalesInvoiceListItem(
        id=voucher.id,
        voucher_no=voucher.voucher_no,
        branch_id=voucher.branch_id,
        document_date=voucher.document_date,
        posting_date=voucher.posting_date,
        status=voucher.status,
        currency_code=voucher.currency_code,
        kind=body.kind,
        customer_id=body.customer_id,
        customer_code=customer_code,
        customer_name=customer_name,
        invoice_form=body.invoice_form,
        invoice_serial=body.invoice_serial,
        invoice_no=body.invoice_no,
        invoice_date=body.invoice_date,
        has_live_einvoice=has_live_einvoice,
        einvoice_serial=einvoice_serial,
        einvoice_no=einvoice_no,
        total_fc=body.total_fc,
        remaining_fc=remaining,
        due_date=due_date,
        days_overdue=days_overdue,
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
