"""Endpoint hóa đơn điện tử và quản lý hóa đơn (`/api/v1/einvoices/*`) — lát 7D.

**Không có `PUT`.** BR-EIV-01 cấm sửa hóa đơn đã phát hành, và hóa đơn chưa phát
hành không có nội dung riêng để sửa (nội dung nó là chứng từ gốc). Bảng chuyển
trạng thái vì thế là toàn bộ bề mặt ghi: lập → phát hành → xác nhận / từ chối →
hủy, mỗi cạnh một endpoint hành động.

Cấp số **đi qua `execute_once`** như mọi lượt ghi khác, và đó là chỗ Open
question #13 được trả lời: lượt gửi lại cùng khóa idempotency **không** đốt
thêm số hóa đơn, vì `execute_once` giành khóa trước khi làm việc và bọc trọn
giao dịch — lượt thua cuộc rollback cả dòng khóa lẫn lượt cấp số, còn lượt gửi
lại đọc kết quả cũ mà không chạy `work` lần nào nữa. Đo bằng
`test_einvoice_numbering.py::test_retrying_issue_with_same_key_does_not_burn_a_number`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SecretBoxes,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.kernel.errors import BranchNotInScopeError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.jobs import queue
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.einvoice import (
    EINVOICE_PERMISSION_MODULE,
    INVOICE_PERMISSION_CODE,
    REGISTRATION_PERMISSION_CODE,
)
from ket.modules.einvoice.models import EInvoice, EInvoiceStatus, OutboxStatus
from ket.modules.einvoice.outbox import due_now, enqueue_issue, list_rows
from ket.modules.einvoice.outbox_job import TRANSMIT_JOB
from ket.modules.einvoice.provider_profile_service import ProviderProfileService
from ket.modules.einvoice.registration_service import InvoiceRegistrationService
from ket.modules.einvoice.schemas import (
    EInvoiceConfirmIn,
    EInvoiceIn,
    EInvoiceIssueIn,
    EInvoiceListOut,
    EInvoiceOut,
    EInvoiceRejectIn,
    ErrorNoticeIn,
    ErrorNoticeOut,
    InvoiceRegistrationIn,
    InvoiceRegistrationOut,
    OutboxListOut,
    OutboxRowOut,
    ProviderProfileIn,
    ProviderProfileOut,
)
from ket.modules.einvoice.service import EInvoiceService

router = APIRouter(prefix="/api/v1/einvoices", tags=["einvoice"])

INVOICE_VIEW = permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.VIEW)
INVOICE_CREATE = permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.CREATE)
INVOICE_EDIT = permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.EDIT)
INVOICE_DELETE = permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.DELETE)
REGISTRATION_VIEW = permission_code(
    EINVOICE_PERMISSION_MODULE, REGISTRATION_PERMISSION_CODE, Action.VIEW
)
REGISTRATION_CREATE = permission_code(
    EINVOICE_PERMISSION_MODULE, REGISTRATION_PERMISSION_CODE, Action.CREATE
)
REGISTRATION_EDIT = permission_code(
    EINVOICE_PERMISSION_MODULE, REGISTRATION_PERMISSION_CODE, Action.EDIT
)

InvoiceReader = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_VIEW))]
InvoiceAuthor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_CREATE))]
InvoiceEditor = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_EDIT))]
InvoiceDeleter = Annotated[AuthorizedRequest, Depends(require_permission(INVOICE_DELETE))]
RegistrationReader = Annotated[AuthorizedRequest, Depends(require_permission(REGISTRATION_VIEW))]
RegistrationAuthor = Annotated[AuthorizedRequest, Depends(require_permission(REGISTRATION_CREATE))]
RegistrationEditor = Annotated[AuthorizedRequest, Depends(require_permission(REGISTRATION_EDIT))]

CREATE_ROUTE: Final[str] = "POST /api/v1/einvoices"
ISSUE_ROUTE: Final[str] = "POST /api/v1/einvoices/{id}/actions/issue"
REGISTRATION_ROUTE: Final[str] = "POST /api/v1/einvoices/registrations"

MAX_PAGE_SIZE: Final[int] = 200
"""Trần một trang, cùng con số với `routers/vouchers.py` — hai lưới của cùng
một hệ thống không nên có hai trần khác nhau."""

CreateKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_ROUTE))]
IssueKey = Annotated[str, Depends(idempotency_key_dependency(ISSUE_ROUTE))]
RegistrationKey = Annotated[str, Depends(idempotency_key_dependency(REGISTRATION_ROUTE))]


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


@router.post("", response_model=EInvoiceOut, status_code=status.HTTP_201_CREATED)
def create_einvoice(
    payload: EInvoiceIn,
    authorized: InvoiceAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreateKey,
    response: Response,
) -> EInvoiceOut:
    """Lập hóa đơn điện tử từ một chứng từ gốc (FR-EIV-010)."""

    def work(session: Session) -> tuple[EInvoiceOut, IdempotentRef]:
        invoice = EInvoiceService(session).create_draft(
            source_voucher_id=payload.source_voucher_id,
            invoice_form_id=payload.invoice_form_id,
        )
        return EInvoiceOut.model_validate(invoice), IdempotentRef(
            result_type=EInvoice.__tablename__, result_id=str(invoice.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> EInvoiceOut:
        return EInvoiceOut.model_validate(EInvoiceService(session).require(UUID(ref.result_id)))

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


@router.post("/{einvoice_id}/actions/issue", response_model=EInvoiceOut)
def issue_einvoice(
    einvoice_id: UUID,
    payload: EInvoiceIssueIn,
    authorized: InvoiceEditor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: IssueKey,
) -> EInvoiceOut:
    """Cấp số, xếp dòng truyền tải, và đánh thức bộ bơm (FR-EIV-013, RT-10).

    Ba việc, **một** transaction. Lượt cấp số và dòng `einvoice_outbox` phải
    cùng commit hoặc cùng không: xem §"giao dịch tới hạn" trong
    `modules/einvoice/outbox.py`. Job bơm xếp cùng chỗ ấy — nó chỉ là lời đánh
    thức, còn công việc thì đã nằm an toàn trên bảng outbox rồi, nên một worker
    chưa chạy chỉ làm hóa đơn đi chậm chứ không làm mất nó.

    Endpoint **không** chờ nhà cung cấp trả lời. Lượt gọi mạng nằm ở worker, và
    đó là điều kiện để một sự cố mạng không kéo theo một request treo — chứng từ
    đã lưu, số đã cấp, tờ hóa đơn nằm trong hàng đợi (FR-NFR-042).
    """

    def work(session: Session) -> tuple[EInvoiceOut, IdempotentRef]:
        provider_code = EInvoiceService(session).provider_code_for(einvoice_id)
        invoice, row = enqueue_issue(
            session,
            einvoice_id,
            invoice_date=payload.invoice_date,
            provider_code=provider_code,
        )
        queue.enqueue(
            session,
            job_type=TRANSMIT_JOB,
            params={"outbox_id": row.id},
            requested_by=authorized.scope.user_id,
            branch_id=invoice.branch_id,
        )
        return EInvoiceOut.model_validate(invoice), IdempotentRef(
            result_type=EInvoice.__tablename__, result_id=str(invoice.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> EInvoiceOut:
        return EInvoiceOut.model_validate(EInvoiceService(session).require(UUID(ref.result_id)))

    body, _ = execute_once(
        factory,
        authorized.scope,
        route_key=ISSUE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    return body


@router.post("/{einvoice_id}/actions/confirm", response_model=EInvoiceOut)
def confirm_einvoice(
    einvoice_id: UUID,
    payload: EInvoiceConfirmIn,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> EInvoiceOut:
    """Cơ quan thuế / nhà cung cấp đã nhận hóa đơn."""
    with unit_of_work(factory, authorized.scope) as session:
        invoice = EInvoiceService(session).confirm(
            einvoice_id,
            tax_authority_code=payload.tax_authority_code,
            lookup_code=payload.lookup_code,
        )
        return EInvoiceOut.model_validate(invoice)


@router.post("/{einvoice_id}/actions/reject", response_model=EInvoiceOut)
def reject_einvoice(
    einvoice_id: UUID,
    payload: EInvoiceRejectIn,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> EInvoiceOut:
    """Bị từ chối. Số đã cấp **giữ nguyên** (ADR-013)."""
    with unit_of_work(factory, authorized.scope) as session:
        invoice = EInvoiceService(session).reject(einvoice_id, message=payload.message)
        return EInvoiceOut.model_validate(invoice)


@router.post("/{einvoice_id}/actions/cancel", response_model=EInvoiceOut)
def cancel_einvoice(
    einvoice_id: UUID,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> EInvoiceOut:
    """Hủy hóa đơn — đòi đủ thông báo hủy và biên bản hủy đã nộp (BR-EIV-04)."""
    with unit_of_work(factory, authorized.scope) as session:
        invoice = EInvoiceService(session).cancel(einvoice_id)
        return EInvoiceOut.model_validate(invoice)


@router.post(
    "/{einvoice_id}/notices", response_model=ErrorNoticeOut, status_code=status.HTTP_201_CREATED
)
def add_error_notice(
    einvoice_id: UUID,
    payload: ErrorNoticeIn,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> ErrorNoticeOut:
    """Lập thông báo hủy hoặc biên bản hủy (FR-EIV-031/032)."""
    with unit_of_work(factory, authorized.scope) as session:
        notice = EInvoiceService(session).add_notice(
            einvoice_id,
            kind=payload.kind,
            notice_no=payload.notice_no,
            notice_date=payload.notice_date,
            reason_code=payload.reason_code,
            reason=payload.reason,
            submitted=payload.submitted,
        )
        return ErrorNoticeOut.model_validate(notice)


@router.post("/notices/{notice_id}/actions/submit", response_model=ErrorNoticeOut)
def submit_error_notice(
    notice_id: UUID,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> ErrorNoticeOut:
    """Đánh dấu văn bản đã nộp cơ quan thuế — vế thứ hai của "lập rồi nộp".

    Thiếu endpoint này thì một văn bản lập ở trạng thái nháp là ngõ cụt: không
    nộp được, không lập lại được (chỉ mục duy nhất theo loại), không xóa được —
    và hóa đơn ấy vĩnh viễn không hủy được (review 7D H-3).
    """
    with unit_of_work(factory, authorized.scope) as session:
        return ErrorNoticeOut.model_validate(EInvoiceService(session).submit_notice(notice_id))


@router.delete("/notices/{notice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_error_notice(
    notice_id: UUID,
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> None:
    """Xóa văn bản **còn nháp** — đường sửa khi lập nhầm loại. Đã nộp thì không."""
    with unit_of_work(factory, authorized.scope) as session:
        EInvoiceService(session).delete_notice(notice_id)


@router.delete("/{einvoice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_einvoice(
    einvoice_id: UUID,
    authorized: InvoiceDeleter,
    factory: SessionFactory,
) -> None:
    """Xóa hóa đơn chưa phát hành."""
    with unit_of_work(factory, authorized.scope) as session:
        EInvoiceService(session).delete(einvoice_id)


@router.get("", response_model=EInvoiceListOut)
def list_einvoices(
    authorized: InvoiceReader,
    factory: SessionFactory,
    status_filter: Annotated[list[EInvoiceStatus] | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> EInvoiceListOut:
    """Một trang hóa đơn, lọc theo trạng thái (FR-EIV-015/025)."""
    with unit_of_work(factory, authorized.scope) as session:
        service = EInvoiceService(session)
        items, total = service.list_by_status(
            statuses=status_filter, page=page, page_size=page_size
        )
        return EInvoiceListOut(
            items=tuple(EInvoiceOut.model_validate(row) for row in items),
            total=total,
            page=page,
            page_size=page_size,
            counts_by_status=service.count_by_status(),
        )


@router.put("/provider-profiles", response_model=ProviderProfileOut)
def put_provider_profile(
    payload: ProviderProfileIn,
    authorized: RegistrationEditor,
    factory: SessionFactory,
    secret_boxes: SecretBoxes,
) -> ProviderProfileOut:
    """Khai thông tin đăng nhập với nhà cung cấp hóa đơn điện tử (FR-EIV-001).

    `PUT` chứ không `POST`: **một dòng cho mỗi nhà cung cấp**, nên khai lại cùng
    mã là sửa hồ sơ đang có. Thao tác tự nó lũy đẳng, đúng lý do nó nằm trong
    danh sách miễn khóa idempotency.

    Quyền của **hồ sơ đăng ký** (2FA) chứ không quyền hóa đơn: khai sai địa chỉ
    máy chủ hay tài khoản là đổi nơi mọi tờ hóa đơn của doanh nghiệp được gửi
    tới — cùng mức hệ quả với việc cấp cho mình một dải số.
    """
    with unit_of_work(factory, authorized.scope) as session:
        profile = ProviderProfileService(session, secret_boxes()).put(
            provider_code=payload.provider_code,
            base_url=payload.base_url,
            username=payload.username,
            password=payload.password,
            tax_code=payload.tax_code,
            is_active=payload.is_active,
        )
        return ProviderProfileOut.model_validate(profile)


@router.get("/provider-profiles", response_model=list[ProviderProfileOut])
def list_provider_profiles(
    authorized: RegistrationReader,
    factory: SessionFactory,
    secret_boxes: SecretBoxes,
) -> list[ProviderProfileOut]:
    """Hồ sơ đã khai — **không** kèm mật khẩu, kể cả dạng đã mã hóa."""
    with unit_of_work(factory, authorized.scope) as session:
        profiles = ProviderProfileService(session, secret_boxes()).list_all()
        return [ProviderProfileOut.model_validate(row) for row in profiles]


@router.post("/outbox/actions/pump", status_code=status.HTTP_202_ACCEPTED)
def pump_outbox(
    authorized: InvoiceEditor,
    factory: SessionFactory,
) -> dict[str, str]:
    """Dọn hàng đợi theo yêu cầu — xếp một lượt bơm không kèm dòng nào cụ thể.

    Đường **duy nhất** đưa một dòng `needs_reconcile` về đích khi chi nhánh
    chưa phát hành thêm hóa đơn nào: `TRANSMIT_JOB` khai `direct_enqueue=False`
    nên `POST /api/v1/jobs` không xếp nó được (cố ý — phát hành hóa đơn là
    quyền có 2FA và không được đi vòng qua endpoint phát hành), và bản cài chưa
    có bộ lập lịch định kỳ nào.

    Quyền `edit` như đường phát hành: lượt bơm gọi tới nhà cung cấp thật.
    """
    with unit_of_work(factory, authorized.scope) as session:
        job = queue.enqueue(
            session,
            job_type=TRANSMIT_JOB,
            requested_by=authorized.scope.user_id,
            branch_id=authorized.scope.acting_branch_id,
        )
        return {"job_id": str(job.id)}


@router.get("/outbox", response_model=OutboxListOut)
def list_outbox(
    authorized: InvoiceReader,
    factory: SessionFactory,
    status_filter: Annotated[OutboxStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> OutboxListOut:
    """Trạng thái hàng đợi truyền tải — panel vận hành (7E-1).

    Khai **trước** `/{einvoice_id}`: FastAPI khớp theo thứ tự khai, nên đặt sau
    thì `outbox` bị đọc như một UUID và endpoint này không bao giờ tới lượt.

    Quyền `view` chứ không `edit`: đây là cửa đọc. Người trực máy cần nhìn thấy
    hàng đợi đang tắc mà không cần quyền phát hành hóa đơn — vốn là quyền có 2FA.
    """
    with unit_of_work(factory, authorized.scope) as session:
        items, total = list_rows(
            session,
            status=status_filter,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return OutboxListOut(
            items=[OutboxRowOut.model_validate(row) for row in items],
            total=total,
            due_now=due_now(session),
        )


@router.get("/{einvoice_id}", response_model=EInvoiceOut)
def get_einvoice(
    einvoice_id: UUID,
    authorized: InvoiceReader,
    factory: SessionFactory,
) -> EInvoiceOut:
    with unit_of_work(factory, authorized.scope) as session:
        return EInvoiceOut.model_validate(EInvoiceService(session).require(einvoice_id))


@router.post(
    "/registrations",
    response_model=InvoiceRegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_registration(
    payload: InvoiceRegistrationIn,
    authorized: RegistrationAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: RegistrationKey,
    response: Response,
) -> InvoiceRegistrationOut:
    """Lập hồ sơ đăng ký / thông báo phát hành (FR-EIV-002, FR-INV-001)."""
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[InvoiceRegistrationOut, IdempotentRef]:
        registration = InvoiceRegistrationService(session).create(
            branch_id=payload.branch_id,
            invoice_form_id=payload.invoice_form_id,
            start_date=payload.start_date,
            notice_no=payload.notice_no,
            notice_date=payload.notice_date,
            quantity=payload.quantity,
            range_from=payload.range_from,
            range_to=payload.range_to,
        )
        return InvoiceRegistrationOut.model_validate(registration), IdempotentRef(
            result_type="invoice_registrations", result_id=str(registration.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> InvoiceRegistrationOut:
        service = InvoiceRegistrationService(session)
        return InvoiceRegistrationOut.model_validate(service.require(UUID(ref.result_id)))

    body, created = execute_once(
        factory,
        authorized.scope,
        route_key=REGISTRATION_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return body


@router.post("/registrations/{registration_id}/actions/activate")
def activate_registration(
    registration_id: UUID,
    authorized: RegistrationEditor,
    factory: SessionFactory,
) -> InvoiceRegistrationOut:
    """Đưa hồ sơ vào hiệu lực; dãy số của ký hiệu được khai ngay tại đây."""
    with unit_of_work(factory, authorized.scope) as session:
        registration = InvoiceRegistrationService(session).activate(registration_id)
        return InvoiceRegistrationOut.model_validate(registration)


@router.get("/registrations/by-form/{invoice_form_id}")
def list_registrations(
    invoice_form_id: int,
    authorized: RegistrationReader,
    factory: SessionFactory,
) -> tuple[InvoiceRegistrationOut, ...]:
    """Hồ sơ của một ký hiệu, trong phạm vi chi nhánh của người gọi (FR-INV-002)."""
    with unit_of_work(factory, authorized.scope) as session:
        rows = InvoiceRegistrationService(session).list_for_form(invoice_form_id=invoice_form_id)
        return tuple(InvoiceRegistrationOut.model_validate(row) for row in rows)
