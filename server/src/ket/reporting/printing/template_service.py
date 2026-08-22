"""Render mẫu in chứng từ (FR-RPT-008) — pha CHỮ đã xong trước khi vào đây.

Gói này KHÔNG đọc chứng từ (C5 cấm reporting→posting): tầng API dựng
`DocumentPrintContext` từ dữ liệu posting rồi đưa sang — mọi giá trị đã là
CHUỖI định dạng sẵn, mẫu chỉ đổ khuôn (cùng nguyên tắc `report_table`).

RT-01: một `SandboxedEnvironment` cho MỌI mẫu (builtin lẫn người dùng sửa),
`asset_url_fetcher` allowlist đóng; `css_extra` của mẫu là CSS không tin cậy —
mọi `url()` trong đó cũng đi qua chính fetcher allowlist nên `file://` chết
cùng một chỗ.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

from ket.kernel.config.printing.context import EMPTY_DETAILS, DocumentPrintDetails
from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.errors import PrintTemplateNotFoundError
from ket.reporting.rendering.environment import (
    create_print_environment,
    make_asset_fetcher,
    print_base_css,
)
from ket.reporting.rendering.header import UnitInfo
from ket.reporting.rendering.options import (
    DEFAULT_FONT_SIZE_PT,
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
)
from ket.reporting.rendering.pdf_renderer import LOGO_ASSET_NAME


@dataclass(frozen=True)
class VoucherPrintLine:
    """Một dòng định khoản trên bản in — mọi ô đã định dạng."""

    line_no: int
    account_code: str
    description: str
    debit: str
    credit: str


@dataclass(frozen=True)
class DocumentPrintContext:
    """Toàn bộ dữ liệu một bản in — hợp đồng giữa tầng API và mẫu.

    Tổng quát hóa ở lát 6E-2 theo hai hướng, mỗi hướng có người dùng thật ngay
    trong lát:

    * `details` mang phần riêng từng loại chứng từ (01-TT cần "Họ và tên người
      nộp tiền", ủy nhiệm chi cần TK người thụ hưởng) — module điền qua
      `PostingDocumentType.print_details`;
    * bản in **không phải chứng từ** cũng đi đường này: biên bản kiểm kê quỹ
      (08a-TT) không có dòng nào trong `vouchers`, nên `lines`/`total_*` để
      rỗng và toàn bộ nội dung nằm ở `details.tables`.

    Tên biến đưa vào mẫu giữ nguyên như lát 5D nên mẫu `PHIEU-KE-TOAN` đã gieo
    trong DB (người dùng có thể đã sửa — FR-RPT-008) vẫn render y hệt.
    """

    title: str
    voucher_no: str
    document_date: str
    posting_date: str
    description: str | None
    draft: bool
    """Chứng từ chưa ghi sổ (FR-RPT-011) — mẫu in dấu BẢN NHÁP."""

    copy_line: str | None
    """`"In lần N"` từ lần in thứ hai (FR-RPT-011); `None` ở lần đầu."""

    lines: tuple[VoucherPrintLine, ...]
    total_debit: str
    total_credit: str
    signature_date_line: str
    unit: UnitInfo
    details: DocumentPrintDetails = EMPTY_DETAILS


def resolve_template(
    session: Session, *, document_type: str, template_code: str | None
) -> PrintTemplate:
    """Mẫu in cho một loại chứng từ: mã tường minh, hoặc mẫu mặc định.

    Mã tường minh phải thuộc ĐÚNG loại chứng từ — một mã hợp lệ của loại khác
    là lỗi, không phải mẫu dùng được: context hai loại chứng từ khác nhau.
    """
    query = select(PrintTemplate).where(PrintTemplate.document_type == document_type)
    if template_code is not None:
        template = session.execute(
            query.where(PrintTemplate.code == template_code)
        ).scalar_one_or_none()
        if template is None:
            raise PrintTemplateNotFoundError(
                "Không có mẫu in này cho loại chứng từ",
                document_type=document_type,
                template_code=template_code,
            )
        return template
    template = session.execute(query.where(PrintTemplate.is_default.is_(True))).scalar_one_or_none()
    if template is None:
        raise PrintTemplateNotFoundError(
            "Loại chứng từ chưa có mẫu in mặc định", document_type=document_type
        )
    return template


def render_document_pdf(
    template: PrintTemplate,
    context: DocumentPrintContext,
    *,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
) -> bytes:
    """Một bản in → PDF theo mẫu — sandbox + allowlist (RT-01); logo + cỡ
    chữ theo cấu hình (FR-RPT-010) đi cùng đường với bản in báo cáo."""
    environment = create_print_environment()
    details = context.details
    html_text = environment.from_string(template.html_template).render(
        title=context.title,
        voucher_no=context.voucher_no,
        document_date=context.document_date,
        posting_date=context.posting_date,
        description=context.description,
        draft=context.draft,
        copy_line=context.copy_line,
        lines=context.lines,
        total_debit=context.total_debit,
        total_credit=context.total_credit,
        signature_date_line=context.signature_date_line,
        unit=context.unit,
        header_fields=details.header_fields,
        fields=details.fields,
        amount=details.amount,
        amount_in_words=details.amount_in_words,
        tables=details.tables,
        notes=details.notes,
        logo_url=f"asset:{LOGO_ASSET_NAME}" if options.logo is not None else None,
    )
    fetcher = make_asset_fetcher(
        {LOGO_ASSET_NAME: (options.logo.content, options.logo.media_type)}
        if options.logo is not None
        else None
    )
    font_config = FontConfiguration()
    stylesheets = [CSS(string=print_base_css(), url_fetcher=fetcher, font_config=font_config)]
    if options.font_size_pt != DEFAULT_FONT_SIZE_PT:
        stylesheets.append(
            CSS(
                string=f"body {{ font-size: {int(options.font_size_pt)}pt }}",
                font_config=font_config,
            )
        )
    if template.css_extra:
        stylesheets.append(
            CSS(string=template.css_extra, url_fetcher=fetcher, font_config=font_config)
        )
    document = HTML(string=html_text, url_fetcher=fetcher, base_url="asset:")
    output = document.write_pdf(stylesheets=stylesheets, font_config=font_config)
    if not isinstance(output, bytes):  # pragma: no cover - hợp đồng weasyprint
        raise TypeError("weasyprint.write_pdf không trả bytes")
    return output
