"""Ba văn bản gửi đối tác, in từ sổ (`/api/v1/partners/{id}/documents/*`, lát 7G-5).

* `doi-chieu-phai-thu` — Biên bản đối chiếu và xác nhận công nợ phải thu (SRS 06 §5.2 #13)
* `doi-chieu-phai-tra` — Biên bản đối chiếu và xác nhận công nợ phải trả (SRS 05 §5 #10)
* `thong-bao-cong-no` — Thông báo công nợ gửi khách hàng (SRS 06 §5.2 #12)

**Đường mẫu in, không phải layout báo cáo** (quyết định 7G-1): thư có chữ ký
hai bên là một mẫu Jinja, không phải lưới cột. **Tính tại chỗ, không lưu**
(quyết định user 2026-09-17): dataset công nợ tính đối trừ TẠI mốc chốt (cắt
theo ngày ghi sổ — xem `ar_ap_open_items.sql`), nên in lại cùng (đối tác, kỳ)
ra cùng số; bản đối tác ký gửi lại lưu qua đính kèm. Không có số văn bản,
không bảng mới, **không ghi `print_log`** — cùng lý do biên bản kiểm kê quỹ:
sổ đếm lần in nói về CHỨNG TỪ.

Ở tầng api (RT-21): đọc danh mục đối tác + dataset công nợ của `receivables`
+ TK ngân hàng doanh nghiệp, và C3 cấm `sales`/`purchase` hỏi hai chủ kia.
Quyền theo chiều, cùng trục với thẻ công nợ (7G-4): `sales.invoice.view` cho
hai văn bản phải thu, `purchase.invoice.view` cho biên bản phải trả — ba
`PrintSubject` đăng ký ở hai module ấy mang đúng mã quyền này, và
`GET /print-templates` liệt kê mẫu theo đó. `master.partners.view` bắt buộc
(không có hồ sơ thì không có "bên B").

Chỉ **sổ tài chính**, cùng sổ với thẻ và guard; phạm vi chi nhánh theo RLS
người in. Biên bản hai chiều lọc theo `partner_id` không ghim `partner_kind`
(đối tác dùng chung mua+bán — xem `partner_overview._items`); thông báo công
nợ chỉ chiều phải thu.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.downloads import content_disposition
from ket.api.open_items import Direction, OpenItem, open_items, period_summary, summarize
from ket.api.render_options import build_render_options
from ket.api.routers.partner_documents_details import notice_details, statement_details
from ket.api.routers.partner_overview import PAYABLE_VIEW, RECEIVABLE_VIEW, load_partner
from ket.api.routers.partners import SPEC as PARTNER_SPEC
from ket.kernel.config.printing.context import DocumentPrintDetails
from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.errors import ReportParamsInvalidError
from ket.kernel.formatting import format_date
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action
from ket.modules.purchase import PAYABLE_STATEMENT_PRINT_CODE
from ket.modules.sales import DEBT_NOTICE_PRINT_CODE, RECEIVABLE_STATEMENT_PRINT_CODE
from ket.reporting.printing.template_service import (
    DocumentPrintContext,
    render_document_pdf,
    resolve_template,
)
from ket.reporting.rendering.header import load_unit_info, signature_date_line

router = APIRouter(prefix="/api/v1/partners", tags=["partners"])

PDF_MEDIA_TYPE: Final[str] = "application/pdf"

PartnerReader = Annotated[
    AuthorizedRequest, Depends(require_permission(PARTNER_SPEC.permission_code(Action.VIEW)))
]

StatementKind = Literal["doi-chieu-phai-thu", "doi-chieu-phai-tra"]

_STATEMENT_DIRECTION: Final[dict[str, Direction]] = {
    "doi-chieu-phai-thu": "thu",
    "doi-chieu-phai-tra": "chi",
}
_STATEMENT_PRINT_CODE: Final[dict[str, str]] = {
    "doi-chieu-phai-thu": RECEIVABLE_STATEMENT_PRINT_CODE,
    "doi-chieu-phai-tra": PAYABLE_STATEMENT_PRINT_CODE,
}
_STATEMENT_PERMISSION: Final[dict[str, str]] = {
    "doi-chieu-phai-thu": RECEIVABLE_VIEW,
    "doi-chieu-phai-tra": PAYABLE_VIEW,
}

_PDF_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    200: {"content": {PDF_MEDIA_TYPE: {}}, "description": "PDF văn bản gửi đối tác"},
    404: {"description": "Không có đối tác / mẫu in"},
}


# Đăng ký TRƯỚC route `{kind}`: FastAPI khớp theo thứ tự khai, và một đường
# dẫn lọt vào route `{kind}` với giá trị ngoài `Literal` là 422 chứ không rơi
# xuống route sau.
@router.post(
    "/{partner_id}/documents/thong-bao-cong-no/print",
    response_class=Response,
    responses=_PDF_RESPONSES,
)
def print_debt_notice(
    partner_id: int,
    authorized: PartnerReader,
    factory: SessionFactory,
    settings: AppSettings,
    as_of: Annotated[date | None, Query()] = None,
    template_code: Annotated[str | None, Query(max_length=50)] = None,
) -> Response:
    """Thông báo công nợ phải thu tại `as_of` (mặc định hôm nay): tổng còn nợ,
    phần quá hạn, từng khoản kèm hạn và số ngày quá hạn, TK nhận tiền."""
    authorized.access.require(RECEIVABLE_VIEW)
    with unit_of_work(factory, authorized.scope) as session:
        partner = load_partner(session, partner_id, authorized)
        template = resolve_template(
            session, document_type=DEBT_NOTICE_PRINT_CODE, template_code=template_code
        )
        # Ngày địa phương của máy chủ, cùng khuôn với `cashflow.py`.
        effective_as_of = as_of or datetime.now(UTC).astimezone().date()
        items = _items(session, "thu", partner, as_of=effective_as_of, open_only=True)
        totals = summarize(items)
        details = notice_details(
            session,
            partner,
            open_items_now=items,
            total_open=totals.open_amount,
            total_overdue=totals.overdue_amount,
        )
        content = _render(
            session,
            authorized,
            settings,
            template_name=template.name,
            template=template,
            document_date=effective_as_of,
            description=f"Tính đến ngày {format_date(effective_as_of)}",
            details=details,
        )
    filename = f"thong-bao-cong-no-{partner.code}-{effective_as_of.isoformat()}.pdf"
    return _pdf_response(content, filename)


@router.post(
    "/{partner_id}/documents/{kind}/print", response_class=Response, responses=_PDF_RESPONSES
)
def print_partner_statement(
    partner_id: int,
    kind: StatementKind,
    authorized: PartnerReader,
    factory: SessionFactory,
    settings: AppSettings,
    from_date: Annotated[date, Query()],
    to_date: Annotated[date, Query()],
    template_code: Annotated[str | None, Query(max_length=50)] = None,
) -> Response:
    """Biên bản đối chiếu công nợ theo kỳ `[from_date, to_date]`.

    Bốn số của kỳ đến từ HAI lượt đọc dataset (`open_only=False`) — tại ngày
    liền trước kỳ và tại cuối kỳ — ghép THEO TỪNG KHOẢN bằng `period_summary`,
    nên đẳng thức đầu + tăng − giảm = cuối đúng bằng cấu trúc, kể cả khi kỳ
    vắt qua lượt chuyển số dư sang niên độ mới. Bảng chi tiết là khoản còn treo
    tại cuối kỳ.

    "In lại cùng (đối tác, kỳ) ra cùng số" đúng khi mọi chứng từ có ngày trong
    kỳ đã ghi sổ: khoản vào biên bản theo NGÀY CHỨNG TỪ còn đối trừ theo NGÀY
    GHI SỔ (luật dataset từ 7A), nên một hóa đơn ghi sổ muộn sẽ đổi tờ đã in.
    """
    if from_date > to_date:
        raise ReportParamsInvalidError(
            "Khoảng thời gian ngược: `from_date` phải không muộn hơn `to_date`",
            param="from_date",
        )
    authorized.access.require(_STATEMENT_PERMISSION[kind])
    direction = _STATEMENT_DIRECTION[kind]
    with unit_of_work(factory, authorized.scope) as session:
        partner = load_partner(session, partner_id, authorized)
        template = resolve_template(
            session, document_type=_STATEMENT_PRINT_CODE[kind], template_code=template_code
        )
        before = _items(
            session, direction, partner, as_of=from_date - timedelta(days=1), open_only=False
        )
        at_close = _items(session, direction, partner, as_of=to_date, open_only=False)
        summary = period_summary(before=before, at=at_close, from_date=from_date, to_date=to_date)
        still_open = [item for item in at_close if item.remaining_fc > 0]
        details = statement_details(
            partner,
            summary=summary,
            open_items_at_close=still_open,
            from_date=from_date,
            to_date=to_date,
        )
        content = _render(
            session,
            authorized,
            settings,
            template_name=template.name,
            template=template,
            document_date=to_date,
            description=f"Kỳ đối chiếu: từ ngày {format_date(from_date)} đến ngày {format_date(to_date)}",
            details=details,
        )
    filename = f"{kind}-{partner.code}-{from_date.isoformat()}-{to_date.isoformat()}.pdf"
    return _pdf_response(content, filename)


def _items(
    session: Session, direction: Direction, partner: Partner, *, as_of: date, open_only: bool
) -> list[OpenItem]:
    return list(
        open_items(
            session, direction=direction, as_of=as_of, partner_id=partner.id, open_only=open_only
        )
    )


def _render(
    session: Session,
    authorized: AuthorizedRequest,
    settings: AppSettings,
    *,
    template_name: str,
    template: PrintTemplate,
    document_date: date,
    description: str,
    details: DocumentPrintDetails,
) -> bytes:
    """Cùng đường với biên bản kiểm kê quỹ: `lines`/`total_*` rỗng, nội dung ở `details`."""
    today = datetime.now(UTC).astimezone().date()
    context = DocumentPrintContext(
        title=template_name,
        voucher_no="",
        document_date=format_date(document_date),
        posting_date=format_date(document_date),
        description=description,
        draft=False,
        copy_line=None,
        lines=(),
        total_debit="",
        total_credit="",
        signature_date_line=signature_date_line(today),
        unit=load_unit_info(session),
        details=details,
    )
    render_options = build_render_options(
        session,
        settings=settings,
        dataset_schema=authorized.scope.dataset_schema,
        user_id=authorized.scope.user_id,
    )
    return render_document_pdf(template, context, options=render_options)


def _pdf_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=PDF_MEDIA_TYPE,
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
