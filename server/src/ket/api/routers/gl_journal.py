"""Endpoint chứng từ nghiệp vụ khác (`/api/v1/gl/journal-vouchers`) — FR-GLE-001.

Tạo và sửa đi qua router riêng của module (màn hình chỉ đọc một module — không
BFF, RT-21); ghi sổ / bỏ ghi sổ / xóa dùng endpoint chứng từ dùng chung
(`routers/vouchers.py`). Đó là ranh giới của phase-04 §API surface: hành động
trạng thái giống nhau cho mọi loại chứng từ, còn thân chứng từ thì mỗi loại
một hình dạng.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
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
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH
from ket.kernel.errors import BranchNotInScopeError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.cash_book.schemas import OpenInvoiceOut, OpenInvoicesResponse
from ket.modules.general_ledger.journal import (
    JOURNAL_PERMISSION_CODE,
    JOURNAL_PERMISSION_MODULE,
)
from ket.modules.general_ledger.journal.models import JournalLine, JournalSettlement
from ket.modules.general_ledger.journal.schemas import (
    JournalLineOut,
    JournalSettlementOut,
    JournalVoucherIn,
    JournalVoucherOut,
    JournalVoucherUpdate,
)
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.debt_lines import DebtSide, classify, is_advance
from ket.posting.documents.models import Voucher
from ket.posting.settlements import open_invoices

router = APIRouter(prefix="/api/v1/gl/journal-vouchers", tags=["general-ledger"])

JOURNAL_VIEW = permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.VIEW)
JOURNAL_CREATE = permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.CREATE)
JOURNAL_EDIT = permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.EDIT)

JournalReader = Annotated[AuthorizedRequest, Depends(require_permission(JOURNAL_VIEW))]
JournalAuthor = Annotated[AuthorizedRequest, Depends(require_permission(JOURNAL_CREATE))]
JournalEditor = Annotated[AuthorizedRequest, Depends(require_permission(JOURNAL_EDIT))]

CREATE_ROUTE: Final[str] = "POST /api/v1/gl/journal-vouchers"
CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    """Chặn sớm với thông điệp chỉ đúng chỗ — RLS `WITH CHECK` là lớp sau."""
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _to_response(
    voucher: Voucher, lines: list[JournalLine], settlements: list[JournalSettlement]
) -> JournalVoucherOut:
    body = JournalVoucherOut.model_validate(voucher)
    return body.model_copy(
        update={
            "lines": tuple(JournalLineOut.model_validate(line) for line in lines),
            "settlements": tuple(JournalSettlementOut.model_validate(row) for row in settlements),
        }
    )


@router.post("", response_model=JournalVoucherOut, status_code=status.HTTP_201_CREATED)
def create_journal_voucher(
    payload: JournalVoucherIn,
    authorized: JournalAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
    acknowledge_warnings: Annotated[bool, Query()] = False,
) -> JournalVoucherOut:
    """Cất chứng từ; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction.

    Khi chế độ Cất-đồng-thời-ghi-sổ bật, người tạo phải có **cả** quyền `post`
    — kiểm trong `work` vì chỉ service mới biết tùy chọn đang bật hay không;
    ném ở đó thì khóa idempotency và số chứng từ cùng rollback.
    `acknowledge_warnings` chỉ có tác dụng trên lượt ghi sổ đi kèm (FR-SYS-062).
    """
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[JournalVoucherOut, IdempotentRef]:
        service = JournalVoucherService(session)
        if value_of(session, key=SAVE_ALSO_POSTS_KEY, user_id=authorized.scope.user_id) is True:
            authorized.access.require(
                permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.POST)
            )
        voucher = service.create(
            payload,
            user_id=authorized.scope.user_id,
            acknowledged_warnings=acknowledge_warnings,
        )
        _, lines, settlements = service.get(voucher.id)
        return _to_response(voucher, lines, settlements), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> JournalVoucherOut:
        voucher, lines, settlements = JournalVoucherService(session).get(UUID(ref.result_id))
        return _to_response(voucher, lines, settlements)

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
def list_journal_open_invoices(
    authorized: JournalReader,
    factory: SessionFactory,
    partner_kind: Annotated[int, Query(ge=0, le=2)],
    partner_id: Annotated[int, Query()],
    account_id: Annotated[int, Query()],
    on_debit: Annotated[bool, Query()],
    currency_code: Annotated[
        str, Query(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    ],
    branch_id: Annotated[int, Query()],
    as_of: Annotated[date, Query()],
) -> OpenInvoicesResponse:
    """Khoản công nợ mà MỘT DÒNG định khoản có thể đối trừ (khối đối trừ 7H-2b).

    Khác ba router trước (`cash-book`/`bank`/`purchase`/`sales`): ở đó chiều đối
    trừ suy từ loại chứng từ, còn dòng GLE mang chiều riêng theo BÊN của nó
    (7C-3/7C-4). Endpoint này vì thế không nhận `side` mà nhận đúng ba thứ dòng
    có — TK, đối tác, bên — rồi chạy **chính `posting.debt_lines.classify`**:
    TK không theo dõi đúng loại đối tác thì dòng không phải dòng công nợ và
    danh sách rỗng (ô người dùng gõ chưa xong là chuyện thường, không phải lỗi);
    có thì bên THUẬN tất toán khoản ứng trước, bên NGƯỢC tất toán khoản nợ.

    Kết quả lọc thêm theo loại đích (`is_advance`), TK của đích và **tiền tệ**
    của dòng để tập trả về **bằng đúng** tập mà `price_settlements` sẽ nhận lúc
    cất — không có dòng nào chọn được rồi 422 (review 7H-2b M-1: ba router
    trước bỏ trục tiền tệ và để 422 nói hộ; ở đây lời hứa là tập đúng).
    """
    _require_branch_in_scope(authorized, branch_id)
    side = DebtSide(
        line_no=1,
        account_id=account_id,
        partner_kind=PartnerKind(partner_kind),
        partner_id=partner_id,
        on_debit=on_debit,
        # Số tiền chỉ để qua phép lọc `amount_fc > 0` của `classify`; danh sách
        # còn nợ không phụ thuộc số tiền dòng.
        amount_fc=Decimal(1),
        currency_code=currency_code,
        exchange_rate=Decimal(1),
    )
    with unit_of_work(factory, authorized.scope) as session:
        debt_lines = classify(session, [side])
        if not debt_lines:
            return OpenInvoicesResponse(items=())
        debt = debt_lines[0]
        invoices = open_invoices(
            session,
            side="receivable" if debt.money_in else "payable",
            partner_kind=debt.partner_kind,
            partner_id=debt.partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )
        return OpenInvoicesResponse(
            items=tuple(
                OpenInvoiceOut.from_invoice(invoice)
                for invoice in invoices
                if is_advance(invoice.target_kind) is debt.settles_advance
                and invoice.account_id == account_id
                and invoice.currency_code == currency_code
            )
        )


@router.get("/{voucher_id}", response_model=JournalVoucherOut)
def get_journal_voucher(
    voucher_id: UUID, authorized: JournalReader, factory: SessionFactory
) -> JournalVoucherOut:
    with unit_of_work(factory, authorized.scope) as session:
        voucher, lines, settlements = JournalVoucherService(session).get(voucher_id)
        return _to_response(voucher, lines, settlements)


@router.put("/{voucher_id}", response_model=JournalVoucherOut)
def update_journal_voucher(
    voucher_id: UUID,
    payload: JournalVoucherUpdate,
    authorized: JournalEditor,
    factory: SessionFactory,
) -> JournalVoucherOut:
    """Sửa chứng từ Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005).

    Không cần khóa idempotency: gửi lại cùng `row_version` thì lần thứ hai đã
    bị chính khóa lạc quan chặn (`409`), và bản thân phép ghi là thay-thế-trọn
    nên không nhân đôi được gì.
    """
    _require_branch_in_scope(authorized, payload.branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        service = JournalVoucherService(session)
        voucher = service.update(
            voucher_id,
            payload,
            expected_row_version=payload.row_version,
            user_id=authorized.scope.user_id,
        )
        _, lines, settlements = service.get(voucher.id)
        return _to_response(voucher, lines, settlements)
