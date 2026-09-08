"""Bản XML hóa đơn gửi EasyInvoice (NĐ123/TT78).

Hình dạng `Invoices/Inv/Invoice` là **của nhà cung cấp**, không phải một chuẩn
mở: tên thẻ, thứ tự và cách viết số đều do máy chủ họ đọc. Tệp này vì thế bám
sát bản tích hợp đang chạy thật ở `~/code/beta.konek.vn`, và mọi chỗ đi chệch
đều có lý do viết ngay tại đó.

**Bản XML KHÔNG mang số hóa đơn.** Nhà cung cấp cấp số ở lượt `issueInvoices` và
trả về trong `KeyInvoiceNo` — quyết định user 2026-09-08, xem `service.issue`.
Ký hiệu và mẫu số đi ở tham số `Pattern`/`Serial` của lời gọi, không nằm trong
thân XML.

**Số tiền viết theo nguyên tệ của hóa đơn**, kèm `ExchangeRate` ở đầu chứng từ.
Quy đổi sang VND ở đây rồi để nhà cung cấp nhân ngược lại là làm tròn hai lần,
và hai lần làm tròn là hai con số — cùng lập luận đã ghi ở `SubledgerEntry`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money_words import amount_in_words
from ket.kernel.protocols import EInvoiceSourceDocument, EInvoiceSourceLine

VAT_EXEMPT = -1
"""Không chịu thuế (KCT)."""
VAT_NOT_DECLARED = -2
"""Không kê khai nộp thuế (KKKNT), **và** ca "dòng không khai thuế suất nào".

Hai thứ khác nhau về pháp lý — quyền khấu trừ thuế đầu vào khác nhau — nhưng
EasyInvoice chỉ có một mã cho cả hai. Ta giữ nguyên sự thật ở phía mình
(`vat_rate IS NULL` khác `vat_rate = 0`) và chỉ gộp ở đúng biên giới này."""

_KNOWN_RATES = frozenset({0, 5, 8, 10})
"""Thuế suất nhà cung cấp nhận. Không phải danh sách ta tự đặt: một con số ngoài
tập này bị máy chủ từ chối, nên gửi đi là chắc chắn hỏng."""


def _money(value: Decimal) -> str:
    """Số tiền dạng chuỗi, làm tròn **nửa lên**, **không** ký hiệu khoa học.

    Hai cái bẫy, cả hai đều im lặng:

    * `round()` của Python làm tròn nửa-về-chẵn, tức ở đúng mốc `.5` nó xuống
      một nửa số lần — lệch cả với thông lệ Việt Nam lẫn với con số đã ghi sổ.
    * `Decimal.normalize()` bỏ số 0 thừa **và** chuyển sang dạng mũ ở đúng
      những con số tròn: một triệu đồng thành `1E+6`. `format(..., "f")` là thứ
      giữ nó ở dạng thập phân — thiếu nó thì bản XML gửi cơ quan thuế mang một
      chuỗi mà không ai đọc ra tiền (bắt được bởi
      `test_the_invoice_xml_carries_the_buyer_and_the_totals`).
    """
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()
    return format(rounded, "f")


def _plain(value: Decimal) -> str:
    """Số **không phải tiền**: số lượng, đơn giá, tỷ giá — giữ nguyên độ chính xác.

    Riêng khỏi `_money` vì ba thứ ấy lưu tới sáu chữ số thập phân (`quantity`,
    `unit_price_fc`, `exchange_rate`), và ép chúng về hai chữ số là làm sai lệch
    chính con số in trên tờ hóa đơn: 0,125 kg thành 0,13 kg, rồi số lượng nhân
    đơn giá không còn ra thành tiền. Chỉ bỏ số 0 thừa, không làm tròn.
    """
    return format(value.normalize(), "f")


def _vat_rate(line: EInvoiceSourceLine) -> int:
    """Thuế suất theo mã EasyInvoice.

    `None` (dòng không khai thuế suất) đi về `KKKNT` chứ không về `0`: khai một
    dòng chưa xác định thành "thuế suất 0%" là một lời khai thuế ta không có căn
    cứ để đưa ra.
    """
    if line.vat_rate is None:
        return VAT_NOT_DECLARED
    rate = int(line.vat_rate)
    if line.vat_rate != rate or rate not in _KNOWN_RATES:
        return VAT_NOT_DECLARED
    return rate


VND = "VND"
"""Đồng Việt Nam — đơn vị duy nhất đọc thành chữ "đồng"."""


def _currency_unit(currency_code: str) -> str:
    """Đơn vị cho phần tiền bằng chữ.

    Hóa đơn ngoại tệ đọc thành chữ theo chính đồng tiền của nó; gắn "đồng" cho
    một tờ hóa đơn USD là ghi sai đơn vị lên chứng từ thuế — và sai theo hướng
    khó thấy, vì con số thì vẫn đúng.
    """
    return "đồng" if currency_code == VND else currency_code


def _address(partner: Partner) -> str:
    parts = (partner.address, partner.district, partner.province)
    return ", ".join(part for part in parts if part)


def build_invoice_xml(
    document: EInvoiceSourceDocument, partner: Partner, *, client_ref: UUID
) -> str:
    """Thân XML của một tờ hóa đơn.

    `client_ref` đi vào thẻ `Ikey` — đó là khóa chống trùng phía nhà cung cấp,
    và cũng chính là thứ `query_status` tra cứu về sau. Một `Ikey` mới cho cùng
    tờ hóa đơn là một tờ hóa đơn thứ hai dưới mắt họ (xem `outbox.py`).
    """
    root = ET.Element("Invoices")
    invoice = ET.SubElement(ET.SubElement(root, "Inv"), "Invoice")

    ET.SubElement(invoice, "Ikey").text = str(client_ref)
    ET.SubElement(invoice, "CusCode").text = partner.code
    ET.SubElement(invoice, "Buyer").text = partner.invoice_recipient or partner.contact_name or ""
    ET.SubElement(invoice, "CusName").text = partner.name
    ET.SubElement(invoice, "CusTaxCode").text = partner.tax_code or ""
    ET.SubElement(invoice, "CusAddress").text = _address(partner)
    # Nhà cung cấp nhận một chuỗi tự do; "TM/CK" (tiền mặt / chuyển khoản) là
    # cách khai chuẩn khi chứng từ chưa chốt hình thức nào — và chứng từ bán ở
    # phase 7 quả thật chưa mang trường ấy.
    ET.SubElement(invoice, "PaymentMethod").text = "TM/CK"
    ET.SubElement(invoice, "ArisingDate").text = document.document_date.strftime("%d/%m/%Y")
    ET.SubElement(invoice, "CurrencyUnit").text = document.currency_code
    ET.SubElement(invoice, "ExchangeRate").text = _plain(document.exchange_rate)

    products = ET.SubElement(invoice, "Products")
    for index, line in enumerate(document.lines, start=1):
        _append_line(products, line, position=index)

    ET.SubElement(invoice, "Total").text = _money(document.total_before_tax_fc)
    ET.SubElement(invoice, "VATAmount").text = _money(document.total_vat_fc)
    ET.SubElement(invoice, "Amount").text = _money(document.total_fc)
    ET.SubElement(invoice, "AmountInWords").text = amount_in_words(
        document.total_fc, unit=_currency_unit(document.currency_code)
    )

    return ET.tostring(root, encoding="unicode")


def _append_line(products: ET.Element, line: EInvoiceSourceLine, *, position: int) -> None:
    product = ET.SubElement(products, "Product")
    ET.SubElement(product, "ProdName").text = line.description
    ET.SubElement(product, "ProdUnit").text = line.unit or ""
    ET.SubElement(product, "ProdQuantity").text = (
        "" if line.quantity is None else _plain(line.quantity)
    )
    ET.SubElement(product, "ProdPrice").text = (
        "" if line.unit_price_fc is None else _plain(line.unit_price_fc)
    )
    ET.SubElement(product, "Discount").text = _money(line.discount_amount_fc)
    ET.SubElement(product, "Total").text = _money(line.amount_fc)
    ET.SubElement(product, "VATRate").text = str(_vat_rate(line))
    ET.SubElement(product, "VATAmount").text = _money(line.vat_amount_fc)
    ET.SubElement(product, "Amount").text = _money(line.amount_fc + line.vat_amount_fc)
    # Thứ tự dòng đi kèm vì bản thể hiện của nhà cung cấp sắp xếp theo nó; thiếu
    # thì các dòng có thể in ra khác thứ tự người lập chứng từ đã gõ.
    ET.SubElement(product, "Extra").text = f'{{"Pos": "{position}"}}'
