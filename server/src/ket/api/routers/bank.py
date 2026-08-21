"""Endpoint chứng từ tiền gửi (`/api/v1/bank/*`) — SRS 04, lát 6C.

Cùng khuôn `routers/cash_book.py`: tạo/sửa/đọc thân chứng từ + picker công nợ
đi qua router của module (màn hình đọc một module — không BFF, RT-21); ghi sổ /
bỏ ghi sổ / xóa dùng endpoint chứng từ dùng chung (`routers/vouchers.py`) —
BC/UNC/SEC/CTNB đã đăng ký loại + hook vòng đời vào registry của posting.
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
)
from ket.api.idempotency import idempotency_key_dependency
from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import BranchNotInScopeError, SettlementSideMismatchError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.bank import (
    BANK_PERMISSION_MODULE,
    CHEQUE_PERMISSION_CODE,
    CREDIT_ADVICE_PERMISSION_CODE,
    INTERNAL_TRANSFER_PERMISSION_CODE,
    PAYMENT_ORDER_PERMISSION_CODE,
)
from ket.modules.bank.models import (
    BankSettlement,
    BankVoucher,
    BankVoucherKind,
    BankVoucherLine,
)
from ket.modules.bank.schemas import (
    BankSettlementOut,
    BankVoucherIn,
    BankVoucherLineOut,
    BankVoucherOut,
    BankVoucherUpdate,
)
from ket.modules.bank.service import BankVoucherService
from ket.modules.bank.settlement_service import open_invoices
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/bank", tags=["bank"])

_PERMISSION_CODE_BY_KIND: Final[dict[int, str]] = {
    BankVoucherKind.CREDIT_ADVICE: CREDIT_ADVICE_PERMISSION_CODE,
    BankVoucherKind.PAYMENT_ORDER: PAYMENT_ORDER_PERMISSION_CODE,
    BankVoucherKind.CHEQUE: CHEQUE_PERMISSION_CODE,
    BankVoucherKind.INTERNAL_TRANSFER: INTERNAL_TRANSFER_PERMISSION_CODE,
}

CREDIT_ADVICE_VIEW = permission_code(
    BANK_PERMISSION_MODULE, CREDIT_ADVICE_PERMISSION_CODE, Action.VIEW
)
PAYMENT_ORDER_VIEW = permission_code(
    BANK_PERMISSION_MODULE, PAYMENT_ORDER_PERMISSION_CODE, Action.VIEW
)
CHEQUE_VIEW = permission_code(BANK_PERMISSION_MODULE, CHEQUE_PERMISSION_CODE, Action.VIEW)

CREATE_ROUTE: Final[str] = "POST /api/v1/bank/vouchers"
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _permission(kind: int, action: Action) -> str:
    return permission_code(BANK_PERMISSION_MODULE, _PERMISSION_CODE_BY_KIND[kind], action)


def _to_response(
    voucher: Voucher,
    body: BankVoucher,
    lines: list[BankVoucherLine],
    settlements: list[BankSettlement],
) -> BankVoucherOut:
    base = BankVoucherOut.model_validate(voucher)
    return base.model_copy(
        update={
            "kind": body.kind,
            "operation_code": body.operation_code,
            "bank_account_id": body.bank_account_id,
            "counter_bank_account_id": body.counter_bank_account_id,
            "partner_id": body.partner_id,
            "partner_kind": body.partner_kind,
            "beneficiary_name": body.beneficiary_name,
            "beneficiary_account_no": body.beneficiary_account_no,
            "beneficiary_bank_name": body.beneficiary_bank_name,
            "cheque_no": body.cheque_no,
            "cheque_date": body.cheque_date,
            "reference_no": body.reference_no,
            "lines": tuple(BankVoucherLineOut.model_validate(line) for line in lines),
            "settlements": tuple(BankSettlementOut.model_validate(row) for row in settlements),
        }
    )


@router.post("/vouchers", response_model=BankVoucherOut, status_code=status.HTTP_201_CREATED)
def create_bank_voucher(
    payload: BankVoucherIn,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> BankVoucherOut:
    """Cất chứng từ tiền gửi; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction.

    `acknowledge_warnings` chỉ có tác dụng trên lượt ghi sổ đi kèm đó (FR-SYS-062
    mức "Cảnh báo" — ví dụ chi vượt số dư TK ngân hàng); mức "Chặn" không mở được.
    """
    _require_branch_in_scope(authorized, payload.branch_id)
    authorized.access.require(_permission(payload.kind, Action.CREATE))

    def work(session: Session) -> tuple[BankVoucherOut, IdempotentRef]:
        service = BankVoucherService(session)
        if value_of(session, key=SAVE_ALSO_POSTS_KEY, user_id=authorized.scope.user_id) is True:
            authorized.access.require(_permission(payload.kind, Action.POST))
        voucher = service.create(
            payload,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledge_warnings,
        )
        header, body, lines, settlements = service.get(voucher.id)
        return _to_response(header, body, lines, settlements), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> BankVoucherOut:
        header, body, lines, settlements = BankVoucherService(session).get(UUID(ref.result_id))
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
    """Picker đối trừ cho chứng từ tiền gửi (FR-BNK-007) — cùng hợp đồng và
    cùng luật chiều↔loại-đối-tác với `/cash-book/open-invoices` (review 6B H-1),
    nhưng cổng quyền theo loại chứng từ tiền gửi: phải thu đòi quyền xem báo
    có, phải trả đòi quyền xem ủy nhiệm chi HOẶC séc (hai loại cùng lập được
    từ danh sách này).
    """
    if side == "receivable":
        authorized.access.require(CREDIT_ADVICE_VIEW)
    elif not (authorized.access.has(PAYMENT_ORDER_VIEW) or authorized.access.has(CHEQUE_VIEW)):
        authorized.access.require(PAYMENT_ORDER_VIEW)
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


@router.get("/vouchers/{voucher_id}", response_model=BankVoucherOut)
def get_bank_voucher(
    voucher_id: UUID, authorized: Authorized, factory: SessionFactory
) -> BankVoucherOut:
    with unit_of_work(factory, authorized.scope) as session:
        header, body, lines, settlements = BankVoucherService(session).get(voucher_id)
        authorized.access.require(_permission(body.kind, Action.VIEW))
        return _to_response(header, body, lines, settlements)


@router.put("/vouchers/{voucher_id}", response_model=BankVoucherOut)
def update_bank_voucher(
    voucher_id: UUID,
    payload: BankVoucherUpdate,
    authorized: Authorized,
    factory: SessionFactory,
) -> BankVoucherOut:
    """Sửa chứng từ Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005)."""
    _require_branch_in_scope(authorized, payload.branch_id)
    authorized.access.require(_permission(payload.kind, Action.EDIT))
    with unit_of_work(factory, authorized.scope) as session:
        service = BankVoucherService(session)
        voucher = service.update(
            voucher_id,
            payload,
            expected_row_version=payload.row_version,
            user_id=authorized.scope.user_id,
        )
        header, body, lines, settlements = service.get(voucher.id)
        return _to_response(header, body, lines, settlements)
