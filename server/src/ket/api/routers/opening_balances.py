"""Số dư ban đầu qua HTTP (`/api/v1/opening-balances`) — slice 4C.

Bốn việc: tải tệp mẫu, xếp lượt kiểm, xếp lượt ghi, và đọc lại dòng đã ghi.
Chuyển số dư sang năm sau **không** có endpoint riêng: job
`posting.opening_balances.carry_forward` khai `direct_enqueue=True` và xếp qua
`POST /api/v1/jobs` — cùng lối job recalc của 4B (tham số tĩnh, quyền tĩnh,
không có lớp phạm vi per-danh-mục nào để endpoint riêng phải kiểm).

Hai endpoint import nằm trong miễn trừ idempotency theo hậu tố
`/import/validate`|`/import/commit` (xem `api/idempotency.py`) — chúng chỉ xếp
hàng, và lượt ghi thay-trọn-nhóm là lũy đẳng theo thiết kế.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy import case, func, select

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.routers.opening_balances_schemas import (
    OpeningBalanceListResponse,
    OpeningBalanceRowResponse,
    OpeningCommitRequest,
    OpeningCommitResponse,
    OpeningInvoiceListResponse,
    OpeningInvoiceResponse,
    OpeningValidateResponse,
)
from ket.kernel.attachments import storage
from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.errors import (
    AttachmentStorageNotConfiguredError,
    OpeningBranchRequiredError,
)
from ket.kernel.excel.template import XLSX_MEDIA_TYPE
from ket.kernel.jobs import queue
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.engine.dimensions import PartnerKind
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.import_job import (
    COMMIT_OPENING_IMPORT,
    OPENING_VIEW,
    OPENING_WRITE,
    VALIDATE_OPENING_IMPORT,
)
from ket.posting.opening_balances.models import OpeningBalance, OpeningBalanceInvoice
from ket.posting.opening_balances.template import (
    TEMPLATE_FILE_NAME,
    build_opening_template,
)
from ket.settings import Settings

router = APIRouter(prefix="/api/v1/opening-balances", tags=["opening-balances"])

OpeningViewer = Annotated[AuthorizedRequest, Depends(require_permission(OPENING_VIEW))]
OpeningWriter = Annotated[AuthorizedRequest, Depends(require_permission(OPENING_WRITE))]

IMPORT_MAX_BYTES: Final[int] = 32 * 1024 * 1024
"""Cùng trần với tệp nhập danh mục (`routers/imports.py`) và cùng lý do: chặn
byte trong lúc ghi, trước khi `reader.MAX_ROWS` kịp đếm dòng."""

MAX_PAGE_SIZE: Final[int] = 200


def _storage_root(settings: Settings) -> Path:
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp (KET_ATTACHMENTS_DIR)"
        )
    return settings.attachments_dir


def _acting_branch_of(authorized: AuthorizedRequest) -> int:
    """Chi nhánh đang thao tác — chặn ngay ở endpoint thay vì để job fail (L3).

    Cùng khuôn `routers/attachments.py`: job chạy dưới phạm vi RLS của chi
    nhánh này, nên thiếu nó thì lượt xếp hàng chỉ là một `202` hứa hão.
    """
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise OpeningBranchRequiredError(
            "Chọn chi nhánh đang thao tác (header X-Branch) trước khi nhập số dư"
        )
    return branch_id


@router.get(
    "/template",
    summary="Số dư ban đầu — tải tệp mẫu nhập liệu",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Tệp .xlsx"}},
)
def download_template(authorized: OpeningViewer) -> Response:
    """Tệp mẫu bốn sheet nhóm 0–4 (FR-OPB-002).

    Quyền `view` chứ không `create`, cùng lý do tệp mẫu danh mục: tệp không
    chứa dữ liệu, và người chuẩn bị tệp thường không phải người bấm nút ghi.
    """
    return Response(
        content=build_opening_template(),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{TEMPLATE_FILE_NAME}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/import/validate",
    response_model=OpeningValidateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Số dư ban đầu — kiểm tra tệp nhập liệu",
)
def validate_import(
    authorized: OpeningViewer,
    factory: SessionFactory,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
    fiscal_year_id: Annotated[int, Form(ge=1)],
    ledger: Annotated[int, Form(ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)] = Ledger.FINANCIAL,
) -> OpeningValidateResponse:
    """Tải tệp lên và xếp lượt kiểm — không ghi gì. Báo cáo ở `jobs.result`."""
    acting_branch = _acting_branch_of(authorized)
    stored = storage.store_stream(
        _storage_root(settings),
        authorized.scope.dataset_schema,
        file.file,
        max_bytes=IMPORT_MAX_BYTES,
    )
    file_name = (file.filename or "so-du-ban-dau.xlsx")[:255]
    with unit_of_work(factory, authorized.scope) as session:
        job = queue.enqueue(
            session,
            job_type=VALIDATE_OPENING_IMPORT,
            params={
                "content_hash": stored.content_hash,
                "file_name": file_name,
                "fiscal_year_id": fiscal_year_id,
                "ledger": ledger,
            },
            requested_by=authorized.scope.user_id,
            branch_id=acting_branch,
        )
        return OpeningValidateResponse(
            job_id=job.id,
            fiscal_year_id=fiscal_year_id,
            ledger=ledger,
            file_name=file_name,
            content_hash=stored.content_hash,
        )


@router.post(
    "/import/commit",
    response_model=OpeningCommitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Số dư ban đầu — ghi dữ liệu đã kiểm",
)
def commit_import(
    payload: OpeningCommitRequest,
    authorized: OpeningWriter,
    factory: SessionFactory,
) -> OpeningCommitResponse:
    """Xếp lượt ghi cho một lượt kiểm đã đạt (H78).

    Thân job kiểm lại lượt kiểm được trỏ tới: đúng loại, đã xong, không dòng
    lỗi, và đúng (năm, sổ) mà endpoint này vừa xét quyền (H89).
    """
    acting_branch = _acting_branch_of(authorized)
    with unit_of_work(factory, authorized.scope) as session:
        job = queue.enqueue(
            session,
            job_type=COMMIT_OPENING_IMPORT,
            params={
                "validation_job_id": str(payload.validation_job_id),
                "fiscal_year_id": payload.fiscal_year_id,
                "ledger": payload.ledger,
            },
            requested_by=authorized.scope.user_id,
            branch_id=acting_branch,
        )
        return OpeningCommitResponse(job_id=job.id)


@router.get("", response_model=OpeningBalanceListResponse)
def list_opening_balances(
    authorized: OpeningViewer,
    factory: SessionFactory,
    fiscal_year_id: Annotated[int, Query(ge=1)],
    ledger: Annotated[int, Query(ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)] = Ledger.FINANCIAL,
    detail_kind: Annotated[int | None, Query(ge=0, le=9)] = None,
    branch_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> OpeningBalanceListResponse:
    """Dòng số dư của một (năm, sổ), kèm hai tổng cân đối (FR-OPB-006).

    RLS lọc chi nhánh trước khi truy vấn này chạy; `branch_id` chỉ thu hẹp thêm
    trong phạm vi đã thấy — cùng hợp đồng với `GET /ledger/trial-balance`.
    """
    partner_code = case(
        (OpeningBalance.partner_kind == PartnerKind.EMPLOYEE, Employee.code),
        else_=Partner.code,
    )
    partner_name = case(
        (OpeningBalance.partner_kind == PartnerKind.EMPLOYEE, Employee.name),
        else_=Partner.name,
    )
    base = (
        select(
            OpeningBalance.id,
            OpeningBalance.detail_kind,
            ChartOfAccount.code.label("account_code"),
            ChartOfAccount.name.label("account_name"),
            partner_code.label("partner_code"),
            partner_name.label("partner_name"),
            OpeningBalance.currency_code,
            OpeningBalance.exchange_rate,
            OpeningBalance.debit_fc,
            OpeningBalance.credit_fc,
            OpeningBalance.debit,
            OpeningBalance.credit,
        )
        .join(ChartOfAccount, OpeningBalance.account_id == ChartOfAccount.id)
        .outerjoin(
            Partner,
            (OpeningBalance.partner_id == Partner.id)
            & OpeningBalance.partner_kind.in_((PartnerKind.CUSTOMER, PartnerKind.VENDOR)),
        )
        .outerjoin(
            Employee,
            (OpeningBalance.partner_id == Employee.id)
            & (OpeningBalance.partner_kind == PartnerKind.EMPLOYEE),
        )
        .where(OpeningBalance.fiscal_year_id == fiscal_year_id)
        .where(OpeningBalance.ledger == ledger)
    )
    if detail_kind is not None:
        base = base.where(OpeningBalance.detail_kind == detail_kind)
    if branch_id is not None:
        base = base.where(OpeningBalance.branch_id == branch_id)

    with unit_of_work(factory, authorized.scope) as session:
        totals_query = (
            select(
                func.coalesce(func.sum(OpeningBalance.debit), 0),
                func.coalesce(func.sum(OpeningBalance.credit), 0),
            )
            .where(OpeningBalance.fiscal_year_id == fiscal_year_id)
            .where(OpeningBalance.ledger == ledger)
        )
        if branch_id is not None:
            totals_query = totals_query.where(OpeningBalance.branch_id == branch_id)
        total_debit, total_credit = session.execute(totals_query).one()

        total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = session.execute(
            base.order_by(OpeningBalance.detail_kind, ChartOfAccount.code, OpeningBalance.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return OpeningBalanceListResponse(
            items=[OpeningBalanceRowResponse.model_validate(row) for row in rows],
            total=int(total),
            page=page,
            page_size=page_size,
            total_debit=Decimal(total_debit),
            total_credit=Decimal(total_credit),
            balanced=Decimal(total_debit) == Decimal(total_credit),
        )


@router.get("/{opening_balance_id}/invoices", response_model=OpeningInvoiceListResponse)
def list_opening_invoices(
    opening_balance_id: int,
    authorized: OpeningViewer,
    factory: SessionFactory,
) -> OpeningInvoiceListResponse:
    """Chi tiết hóa đơn/tạm ứng của một dòng số dư (FR-OPB-003).

    JOIN qua dòng cha là bắt buộc: RLS chi nhánh nằm trên `opening_balances`,
    còn bảng con không có cột chi nhánh — thiếu JOIN thì id đoán được của chi
    nhánh khác cũng đọc ra hóa đơn.
    """
    with unit_of_work(factory, authorized.scope) as session:
        rows = (
            session.execute(
                select(OpeningBalanceInvoice)
                .join(
                    OpeningBalance,
                    OpeningBalanceInvoice.opening_balance_id == OpeningBalance.id,
                )
                .where(OpeningBalance.id == opening_balance_id)
                .order_by(OpeningBalanceInvoice.invoice_date, OpeningBalanceInvoice.invoice_no)
            )
            .scalars()
            .all()
        )
        return OpeningInvoiceListResponse(
            items=[OpeningInvoiceResponse.model_validate(row) for row in rows]
        )
