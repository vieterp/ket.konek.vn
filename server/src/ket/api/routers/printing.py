"""In chứng từ + danh mục mẫu in (FR-RPT-008/011) — lát 5D.

| Đường dẫn | Việc | Quyền |
| --- | --- | --- |
| `GET  /api/v1/print-templates` | Mẫu in theo loại chứng từ | `…{loại}.print` |
| `POST /api/v1/vouchers/{id}/print` | Kết xuất PDF + ghi `print_log` | `…{loại}.print` |

Đứng ở tầng API có chủ đích: việc in cần CẢ chứng từ (`ket.posting`) lẫn mẫu +
render (`ket.reporting`), mà C5 cấm reporting→posting — tầng API là chỗ duy
nhất được phép ráp hai bên (cùng vị trí với `routers/vouchers.py`).

**Không** khai idempotency key: in lần 2 là một SỰ KIỆN THẬT mà FR-RPT-011
sinh ra để đếm (`copy_no` + cảnh báo), không phải một lần gửi lại cần khử.
Quyền kiểm bên trong transaction vì mã quyền phụ thuộc `document_type` đọc từ
DB — cùng lý do `routers/vouchers.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, Authorized, SessionFactory
from ket.api.render_options import build_render_options
from ket.api.routers.printing_schemas import (
    PrintTemplateListResponse,
    PrintTemplateSummaryResponse,
    VoucherPrintRequest,
)
from ket.kernel import formatting as formats
from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.catalog import (
    MONEY_SCALE_KEY,
    PRINT_ALLOW_DRAFT_KEY,
    PRINT_ALLOW_LOCKED_KEY,
)
from ket.kernel.config.printing.context import EMPTY_DETAILS
from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import PrintNotAllowedError, VoucherNotFoundError
from ket.kernel.money import convert_currency
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action
from ket.posting.contracts import POSTING_DOCUMENT_REGISTRY, Voucher, VoucherStatus
from ket.reporting.printing.models import PrintLog
from ket.reporting.printing.template_service import (
    DocumentPrintContext,
    VoucherPrintLine,
    render_document_pdf,
    resolve_template,
)
from ket.reporting.rendering.header import load_unit_info, signature_date_line

router = APIRouter(prefix="/api/v1", tags=["printing"])

PDF_MEDIA_TYPE = "application/pdf"


@router.get("/print-templates", response_model=PrintTemplateListResponse)
def list_print_templates(
    authorized: Authorized,
    factory: SessionFactory,
    document_type: Annotated[str | None, Query(max_length=20)] = None,
) -> PrintTemplateListResponse:
    """Mẫu in đã đăng ký — hộp chọn mẫu của nút In.

    Không nêu `document_type` thì trả mẫu của những loại người gọi in được —
    cùng luật danh-sách-trộn-không-vòng-qua-phân-quyền với `list_vouchers`.
    """
    if document_type is not None:
        allowed_types = [document_type]
        authorized.access.require(
            POSTING_DOCUMENT_REGISTRY.get(document_type).permission(Action.PRINT)
        )
    else:
        allowed_types = [
            code
            for code in POSTING_DOCUMENT_REGISTRY.codes()
            if authorized.access.has(POSTING_DOCUMENT_REGISTRY.get(code).permission(Action.PRINT))
        ]
        if not allowed_types:
            return PrintTemplateListResponse(templates=[])
    with unit_of_work(factory, authorized.scope) as session:
        rows = (
            session.execute(
                select(PrintTemplate)
                .where(PrintTemplate.document_type.in_(allowed_types))
                .order_by(PrintTemplate.document_type, PrintTemplate.code)
            )
            .scalars()
            .all()
        )
        return PrintTemplateListResponse(
            templates=[PrintTemplateSummaryResponse.model_validate(row) for row in rows]
        )


@router.post(
    "/vouchers/{voucher_id}/print",
    response_class=Response,
    responses={
        200: {
            "content": {PDF_MEDIA_TYPE: {}},
            "description": (
                "PDF bản in; `X-Print-Copy-No` = lần in thứ mấy, "
                "`X-Print-Reprint: true` từ lần thứ hai (FR-RPT-011)"
            ),
        },
        404: {"description": "Không có chứng từ / mẫu in"},
        409: {"description": "Chứng từ ở trạng thái không in được"},
    },
)
def print_voucher(
    voucher_id: UUID,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    body: VoucherPrintRequest,
) -> Response:
    """In một chứng từ theo mẫu (FR-RPT-008): sandbox + allowlist (RT-01),
    chứng từ chưa ghi sổ mang dấu BẢN NHÁP, mỗi lần in một dòng `print_log`
    (FR-RPT-011). Khóa dòng chứng từ (`FOR UPDATE`) nên `copy_no` nối tiếp
    nhau kể cả khi hai người cùng bấm In."""
    with unit_of_work(factory, authorized.scope) as session:
        voucher = _require_locked_voucher(session, voucher_id)
        document_type = POSTING_DOCUMENT_REGISTRY.get(voucher.document_type)
        authorized.access.require(document_type.permission(Action.PRINT))
        _guard_printable(session, voucher, user_id=authorized.scope.user_id)
        template = resolve_template(
            session, document_type=voucher.document_type, template_code=body.template_code
        )
        copy_no = _next_copy_no(session, voucher_id)
        context = _build_context(
            session,
            voucher,
            copy_no=copy_no,
            title=template.name,
            user_id=authorized.scope.user_id,
        )
        render_options = build_render_options(
            session,
            settings=settings,
            dataset_schema=authorized.scope.dataset_schema,
            user_id=authorized.scope.user_id,
        )
        content = render_document_pdf(template, context, options=render_options)
        session.add(
            PrintLog(
                voucher_id=voucher.id,
                branch_id=voucher.branch_id,
                template_code=template.code,
                copy_no=copy_no,
                printed_by=authorized.scope.user_id,
            )
        )
    filename = f"{_safe_file_stem(voucher.voucher_no)}-lan-{copy_no}.pdf"
    return Response(
        content=content,
        media_type=PDF_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "X-Print-Copy-No": str(copy_no),
            "X-Print-Reprint": "true" if copy_no > 1 else "false",
        },
    )


def _safe_file_stem(value: str) -> str:
    """`voucher_no` vào header `Content-Disposition` — hôm nay định dạng số do
    mã nguồn kiểm soát, nhưng FR-SYS-063 sẽ biến prefix thành dữ liệu gói cấu
    hình (review 5D, L5): lọc trước để một quy tắc đánh số kỳ dị không tiêm
    được ký tự điều khiển/dấu nháy vào header."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value) or "chung-tu"


def _guard_printable(session: Session, voucher: Voucher, *, user_id: int) -> None:
    """FR-RPT-011: cho/không cho in chứng từ chưa ghi sổ và chứng từ kỳ đã
    khóa — hai công tắc cấu hình, mặc định đều cho phép (nháp mang watermark).
    """
    if voucher.status == VoucherStatus.DA_HUY:
        raise PrintNotAllowedError(
            "Chứng từ đã hủy — không in được nữa", voucher_no=voucher.voucher_no
        )
    if voucher.status == VoucherStatus.DA_CAT and (
        value_of(session, key=PRINT_ALLOW_DRAFT_KEY, user_id=user_id) is not True
    ):
        raise PrintNotAllowedError(
            "Đơn vị đã tắt in chứng từ chưa ghi sổ — ghi sổ trước rồi in",
            voucher_no=voucher.voucher_no,
        )
    if value_of(session, key=PRINT_ALLOW_LOCKED_KEY, user_id=user_id) is not True:
        locked_at = session.execute(
            select(AccountingPeriod.locked_at).where(AccountingPeriod.id == voucher.period_id)
        ).scalar_one_or_none()
        if locked_at is not None:
            raise PrintNotAllowedError(
                "Đơn vị đã tắt in chứng từ thuộc kỳ đã khóa sổ",
                voucher_no=voucher.voucher_no,
            )


def _require_locked_voucher(session: Session, voucher_id: UUID) -> Voucher:
    """Đọc + khóa dòng chứng từ — `VoucherService.require` không khóa, mà
    `copy_no` đếm-rồi-ghi cần nối tiếp (ràng buộc duy nhất của `print_log` là
    hàng rào cuối, không phải cơ chế)."""
    voucher = session.execute(
        select(Voucher).where(Voucher.id == voucher_id).with_for_update()
    ).scalar_one_or_none()
    if voucher is None:
        raise VoucherNotFoundError("Không tìm thấy chứng từ", voucher_id=str(voucher_id))
    return voucher


def _next_copy_no(session: Session, voucher_id: UUID) -> int:
    printed = (
        session.execute(
            select(func.count()).select_from(PrintLog).where(PrintLog.voucher_id == voucher_id)
        ).scalar_one()
        or 0
    )
    return printed + 1


def _build_context(
    session: Session, voucher: Voucher, *, copy_no: int, title: str, user_id: int
) -> DocumentPrintContext:
    """Dữ liệu chứng từ → context CHUỖI định dạng sẵn cho mẫu.

    Dòng in là ĐỊNH KHOẢN SỔ TÀI CHÍNH quy đổi VND — đọc qua chính
    `build_request` mà nút Ghi sổ dùng, và quy đổi bằng đúng `money.scale` mà
    đường ghi sổ dùng, nên bản in nháp và dòng sẽ lên sổ là một bộ số (không
    có đường "in một đằng, ghi một nẻo").
    """
    document_type = POSTING_DOCUMENT_REGISTRY.get(voucher.document_type)
    request = document_type.build_request(session, voucher.id)
    details = (
        EMPTY_DETAILS
        if document_type.print_details is None
        else document_type.print_details(session, voucher.id, user_id)
    )
    scale_value = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    scale = scale_value if isinstance(scale_value, int) else 2
    codes = _account_codes(session, {line.account_id for line in request.financial_lines})
    lines = []
    total_debit = Decimal(0)
    total_credit = Decimal(0)
    for index, line in enumerate(request.financial_lines, start=1):
        debit = convert_currency(line.debit_fc, line.rate, scale)
        credit = convert_currency(line.credit_fc, line.rate, scale)
        total_debit += debit
        total_credit += credit
        lines.append(
            VoucherPrintLine(
                line_no=index,
                account_code=codes.get(line.account_id, "?"),
                description=line.description or voucher.description or "",
                debit=formats.format_money(debit, blank_zero=True),
                credit=formats.format_money(credit, blank_zero=True),
            )
        )
    today = datetime.now(UTC).astimezone().date()
    return DocumentPrintContext(
        title=title,
        voucher_no=voucher.voucher_no,
        document_date=formats.format_date(voucher.document_date),
        posting_date=formats.format_date(voucher.posting_date),
        description=voucher.description,
        draft=voucher.status == VoucherStatus.DA_CAT,
        copy_line=f"In lần {copy_no}" if copy_no > 1 else None,
        lines=tuple(lines),
        total_debit=formats.format_money(total_debit, blank_zero=False),
        total_credit=formats.format_money(total_credit, blank_zero=False),
        signature_date_line=signature_date_line(today),
        unit=load_unit_info(session),
        details=details,
    )


def _account_codes(session: Session, account_ids: set[int]) -> dict[int, str]:
    if not account_ids:
        return {}
    rows = session.execute(
        select(ChartOfAccount.id, ChartOfAccount.code).where(ChartOfAccount.id.in_(account_ids))
    ).all()
    return {row.id: row.code for row in rows}
