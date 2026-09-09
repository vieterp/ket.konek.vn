"""Nội dung bản thể hiện hóa đơn cho mẫu in (FR-EIV-026, lát 7E-3).

Cùng khuôn `modules/cash_book/print_details.py`: module dựng **dữ liệu** in,
tầng `api` dựng **tờ giấy** (C5 cấm `modules` chạm `reporting`). Khác một điểm
và điểm ấy đáng nói: bản thể hiện **không phải chứng từ** — nó không có dòng nào
trong `vouchers` — nên nó đi đường `PrintSubject` của registry "bản in không
phải chứng từ" (6E-2), giống biên bản kiểm kê quỹ.

**Chỉ dùng cho hóa đơn phát hành nội bộ.** Hóa đơn qua nhà cung cấp lấy bản thể
hiện *của họ* (`getInvoicePdf`) — tờ giấy có giá trị là tờ khớp bản XML đã ký,
và ta không dựng lại được nó cho đúng. Ở đây là hóa đơn đặt in / tự in, nơi bản
gốc vốn là tờ giấy do chính phần mềm in ra.

Nội dung đọc qua Protocol `EInvoiceSource` (ADR-022) — cùng nguồn với bộ dựng
XML của 7E-2, nên tờ giấy và tệp XML không thể nói hai con số khác nhau.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.printing.context import (
    DocumentPrintDetails,
    PrintField,
    PrintTable,
    PrintTableColumn,
)
from ket.kernel.config.printing.voucher_fields import RATE_DECIMALS
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import EInvoiceRepresentationUnavailableError
from ket.kernel.formatting import format_date, format_money, format_quantity
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money import MONEY_SCALE_DEFAULT, convert_currency
from ket.kernel.money_words import VND, amount_in_words, currency_unit
from ket.kernel.periods.service import base_currency_of_period
from ket.kernel.protocols import PROVIDERS as CROSS_MODULE
from ket.kernel.protocols import EInvoiceSourceDocument
from ket.modules.einvoice.models import EInvoice, EInvoiceStatus
from ket.posting.documents.models import Voucher

_COLUMNS: tuple[PrintTableColumn, ...] = (
    PrintTableColumn(label="STT", align="center", width="5%"),
    PrintTableColumn(label="Tên hàng hóa, dịch vụ"),
    PrintTableColumn(label="Đơn vị tính", align="center", width="9%"),
    PrintTableColumn(label="Số lượng", align="money", width="9%"),
    PrintTableColumn(label="Đơn giá", align="money", width="13%"),
    PrintTableColumn(label="Thành tiền", align="money", width="14%"),
    # Thuế suất là nội dung **bắt buộc theo dòng** của NĐ123 §10, không phải một
    # con số tổng: một tờ hóa đơn có cả dòng 10%, dòng 5% và dòng không chịu
    # thuế là hình dạng thường, và bỏ cột này thì người mua không đối chiếu
    # được thuế đầu vào của từng dòng. Bản đầu của lát này chỉ in tổng tiền
    # thuế, trong khi chú thích của chính mẫu in khai là có phủ thuế suất.
    PrintTableColumn(label="Thuế suất", align="center", width="8%"),
    PrintTableColumn(label="Tiền thuế", align="money", width="12%"),
)

_NOT_DECLARED = "KKKNT"
"""Nhãn cho dòng **chưa khai thuế suất** — khác hẳn "0%".

Cùng luật mà 7E-2 đặt cho bộ dựng XML: `vat_rate = None` là "không kê khai",
không phải "thuế suất 0%", và hai thứ ấy khác nhau về quyền khấu trừ đầu vào."""


def build_representation_details(
    session: Session, invoice: EInvoice, *, user_id: int
) -> DocumentPrintDetails:
    """Sáu vùng của bản thể hiện, mọi ô đã là chuỗi định dạng sẵn."""
    document = _read_source(session, invoice)
    partner = session.get(Partner, document.partner_id)
    form = session.get(InvoiceForm, invoice.invoice_form_id)

    header_fields = (
        PrintField(label="Mẫu số", value=form.form_no or "" if form is not None else ""),
        PrintField(label="Ký hiệu", value=form.code if form is not None else ""),
        # Số còn trống ở những trạng thái hợp lệ mà chưa có số (7E-2) — in ô
        # rỗng chứ không in chữ "chưa có": tờ giấy này là bản thể hiện của một
        # hóa đơn, và một dòng chú thích kỹ thuật không thuộc về nó.
        PrintField(label="Số", value=invoice.invoice_no or ""),
    )

    fields = (
        PrintField(label="Ngày hóa đơn", value=format_date(invoice.invoice_date)),
        PrintField(label="Đơn vị mua hàng", value=partner.name if partner is not None else ""),
        PrintField(label="Mã số thuế", value=partner.tax_code or "" if partner else ""),
        PrintField(label="Địa chỉ", value=partner.full_address if partner else ""),
        PrintField(label="Đồng tiền thanh toán", value=document.currency_code),
    )

    rows = tuple(
        (
            str(index),
            line.description,
            line.unit or "",
            format_quantity(line.quantity),
            format_money(line.unit_price_fc, blank_zero=True),
            format_money(line.amount_fc, blank_zero=False),
            _vat_rate_label(line.vat_rate),
            format_money(line.vat_amount_fc, blank_zero=False),
        )
        for index, line in enumerate(document.lines, start=1)
    )

    table = PrintTable(
        columns=_COLUMNS,
        rows=rows,
        total_row=(
            "",
            "Cộng",
            "",
            "",
            "",
            format_money(document.total_before_tax_fc, blank_zero=False),
            "",
            format_money(document.total_vat_fc, blank_zero=False),
        ),
    )

    notes = [
        PrintField(
            label="Tổng tiền chưa thuế",
            value=format_money(document.total_before_tax_fc, blank_zero=False),
        ),
        PrintField(
            label="Tổng tiền thuế GTGT",
            value=format_money(document.total_vat_fc, blank_zero=False),
        ),
        PrintField(
            label="Tổng tiền thanh toán",
            value=format_money(document.total_fc, blank_zero=False),
        ),
    ]
    base_currency = _base_currency(session, invoice)
    if document.currency_code != base_currency:
        # NĐ123 §10 khoản 13: hóa đơn ngoại tệ phải ghi **tỷ giá** cùng số tiền
        # quy đổi ra đồng Việt Nam. Cả hai dữ kiện đã có sẵn trong Protocol, nên
        # bỏ chúng đi là bỏ một nội dung bắt buộc chứ không phải một tiện ích.
        notes.extend(
            (
                PrintField(
                    label="Tỷ giá",
                    value=format_quantity(document.exchange_rate, decimals=RATE_DECIMALS),
                ),
                PrintField(
                    label=f"Tổng tiền thanh toán quy đổi ({base_currency})",
                    value=format_money(
                        _converted_total(session, document, user_id=user_id), blank_zero=False
                    ),
                ),
            )
        )

    return DocumentPrintDetails(
        header_fields=header_fields,
        fields=fields,
        amount=format_money(document.total_fc, blank_zero=False),
        # Đọc theo **đồng tiền của hóa đơn**, không gắn "đồng" cho tờ ngoại tệ —
        # cùng luật mà 7E-2 đã đặt cho bộ dựng XML, và cùng một lý do: dòng này
        # là dòng người mua đọc để đối chiếu số tiền phải trả.
        amount_in_words=amount_in_words(
            document.total_fc, unit=currency_unit(document.currency_code)
        ),
        tables=(table,),
        notes=tuple(notes),
    )


_VOIDED_LABELS: dict[EInvoiceStatus, str] = {
    EInvoiceStatus.DA_THAY_THE: "HÓA ĐƠN ĐÃ BỊ THAY THẾ",
    EInvoiceStatus.DA_DIEU_CHINH: "HÓA ĐƠN ĐÃ BỊ ĐIỀU CHỈNH",
    EInvoiceStatus.DA_HUY: "HÓA ĐƠN ĐÃ HỦY",
}
"""Ba trạng thái mà tờ giấy **không còn là một hóa đơn còn hiệu lực**.

Quyết định user 2026-09-09: vẫn in được (hồ sơ lưu trữ cần, kiểm toán viên hỏi
tới), nhưng phải mang dấu — một tờ đã hủy không phân biệt được với tờ còn hiệu
lực là thứ đi ra ngoài rồi không thu lại được.

**Bất đối xứng nói thẳng:** dấu này chỉ đóng được lên bản ta DỰNG, tức hóa đơn
phát hành nội bộ. Hóa đơn qua nhà cung cấp trả về PDF **của họ** và ta không
sửa được tờ ấy — nợ ghi lại cho lát UI (7H), nơi màn hình hiện trạng thái cạnh
nút tải."""


def voided_label(status: EInvoiceStatus) -> str | None:
    """Nhãn trạng thái cần đóng lên bản in, hoặc `None` nếu tờ còn hiệu lực."""
    return _VOIDED_LABELS.get(status)


def _read_source(session: Session, invoice: EInvoice) -> EInvoiceSourceDocument:
    """Nội dung chứng từ gốc, qua Protocol dùng chung với bộ dựng XML.

    Không đọc được thì **404 chứ không 500**: phân hệ sở hữu loại chứng từ ấy
    chưa khai nguồn hóa đơn là một cấu hình thiếu, và câu trả lời đúng cho người
    dùng là "tờ hóa đơn này chưa có bản thể hiện", kèm lý do.
    """
    for source in CROSS_MODULE.einvoice_sources():
        document = source.read(session, voucher_id=invoice.source_voucher_id)
        if document is not None:
            return document
    raise EInvoiceRepresentationUnavailableError(
        "Không đọc được nội dung chứng từ gốc nên chưa dựng được bản thể hiện",
        einvoice=str(invoice.id),
    )


def _vat_rate_label(rate: Decimal | None) -> str:
    """Ô thuế suất của một dòng.

    `None` đi về nhãn "không kê khai", không về `0%`: khai một dòng chưa xác
    định thành "thuế suất 0%" là lời khai thuế không có căn cứ, và hai thứ ấy
    khác nhau về quyền khấu trừ đầu vào của người mua.

    **Không dùng `format_quantity`** — bản đầu của lát này dùng, và sai hai
    đường trên cùng một dòng. `format_quantity` trả chuỗi **rỗng** khi giá trị
    bằng 0 (đúng với cột số lượng, sai chết người ở đây: hàng xuất khẩu chịu
    thuế suất `0%` in ra một dấu `%` trơ trọi, mà `0%` chính là giá trị mà cả
    `_NOT_DECLARED` lẫn bộ dựng XML 7E-2 dựng ra để phân biệt). Và `decimals=0`
    **làm tròn**: `8,25%` in thành `8%`, `4,5%` thành `5%` — cả hai đều là giá
    trị hợp lệ mà API nhận (`VAT_RATE_SCALE = 2`), và cả hai đều là con số của
    pháp luật in trên chứng từ thuế.

    `:f` chứ không `str()`: `Decimal("10").normalize()` là `1E+1`, và bẫy ấy đã
    một lần biến một triệu đồng thành `1E+6` trong bản XML ở 7E-2.
    """
    if rate is None:
        return _NOT_DECLARED
    return f"{rate.normalize():f}%".replace(".", ",")


def _base_currency(session: Session, invoice: EInvoice) -> str:
    """Đồng tiền hạch toán của kỳ chứa chứng từ gốc.

    So với **đồng hạch toán**, không so cứng với `"VND"`: `voucher_fields` đã
    đặt luật ấy cho khối "Tỷ giá ngoại tệ / Số tiền quy đổi" của phiếu thu chi,
    và hai bản in cùng nói về một chứng từ thì không được dùng hai định nghĩa
    "thế nào là ngoại tệ".

    Không đọc được kỳ (chứng từ đã biến mất giữa chừng) thì lùi về `VND` — bản
    cài v1 hạch toán bằng đồng Việt Nam, nên đó là phỏng đoán an toàn và nó chỉ
    quyết định **có in thêm hai dòng hay không**, không đổi con số nào.
    """
    voucher = session.get(Voucher, invoice.source_voucher_id)
    if voucher is None:
        return VND
    return base_currency_of_period(session, voucher.period_id)


def _converted_total(
    session: Session, document: EInvoiceSourceDocument, *, user_id: int
) -> Decimal:
    """Tổng tiền thanh toán quy đổi — làm tròn **theo từng dòng**, rồi mới cộng.

    Bản đầu của lát này viết `total_fc * exchange_rate` và in ra một con số VND
    có tám chữ số thập phân. Hai lỗi trong một biểu thức: không làm tròn, và
    quy đổi trên **số tổng**.

    `voucher_fields.foreign_currency_notes` đã nói vì sao chiều thứ hai cũng
    sai: `PostingService` gọi `convert_currency` cho **từng** dòng rồi mới cộng,
    nên quy đổi trên số tổng lệch số đã ghi sổ đúng bằng phần làm tròn — và một
    bản in nói khác sổ là một bản in sai. Doanh thu và thuế đi vào hai dòng bút
    toán khác nhau nên chúng quy đổi riêng.
    """
    scale_value = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    scale = scale_value if isinstance(scale_value, int) else MONEY_SCALE_DEFAULT
    rate = document.exchange_rate
    return sum(
        (
            convert_currency(amount, rate, scale)
            for line in document.lines
            for amount in (line.amount_fc, line.vat_amount_fc)
            if amount
        ),
        Decimal(0),
    )
