"""Phân giải tệp XML hóa đơn điện tử **đầu vào** theo chuẩn TCT (FR-EIV-040).

Đây là chiều ngược của `providers/easyinvoice/xml_builder.py`, và hai chiều
**không** đối xứng — nên tệp này không tái dùng gì của tệp kia:

* Chiều **gửi** dựng hình dạng `Invoices/Inv/Invoice` **của nhà cung cấp**: tên
  thẻ do máy chủ họ đọc, không phải một chuẩn mở.
* Chiều **nhận** đọc hình dạng `HDon/DLHDon` của **Tổng cục Thuế** (TT78/QĐ1450)
  — thứ mọi tờ hóa đơn hợp lệ đều mang, bất kể người bán dùng nhà cung cấp nào.

**Tên thẻ ở đây không đoán theo tài liệu.** Chúng lấy từ **phần chuẩn TCT
của** bản tích hợp đang chạy thật ở `~/code/beta.konek.vn`
(`konek_accounting_auto_bill_email`), cùng nguồn mà bộ dựng XML chiều gửi đã bám
theo — kèm ba thứ chỉ dữ liệu thật mới dạy: namespace lúc có lúc không (nên
khớp theo tên cục bộ), `TSuat` mang hậu tố `%`, và hóa đơn điện/nước **không
có** `DGia`.

**Chỉ phần chuẩn ấy, và đó là một lượt cắt có chủ đích** (user chốt 2026-09-12).
Bản tích hợp kia thử nhiều tên cho mỗi trường (`SHDon|So|InvNo|SoHD`,
`KHHDon|KyHieu|Serial`, `NLap|NgayHD|Ngay`…). Không chép bộ alias ấy sang: mỗi
alias là một phỏng đoán không tệp thật nào chứng minh, và một bộ đọc nhận ba
tên cho cùng một trường là ba đường để hai tệp của **cùng một tờ hóa đơn** đọc
ra hai danh tính khác nhau — tức thủng chính khóa khử trùng. `KHMSHDon` vì thế
cũng **bắt buộc** dù bản kia không đọc nó ở đâu: nó nằm trong khóa ấy.

**Một lỗi của bản tích hợp ấy KHÔNG được chép sang.** Bộ đọc số của nó bỏ mọi
dấu chấm rồi đổi phẩy thành chấm — tức đoán định dạng Việt Nam cho **mọi** con
số. XML chuẩn TCT viết số thập phân bằng dấu chấm, nên `4545455.50` đi qua đó
thành `454545550`: sai gấp một trăm lần, im lặng, trên một con số tiền.
`_number` dưới đây vì thế **từ chối** mọi thứ không phải số thập phân chuẩn
thay vì đoán, và `test_a_decimal_point_is_not_a_thousands_separator` ghim nó.

**Mọi giá trị đọc ra đều bị chặn ĐỘ DÀI và ĐỘ LỚN tại đây**, không để tầng
bảng chặn hộ. Cột `varchar(20)` và `numeric(5,2)` mà nhận thẳng chuỗi của người
tải lên thì một `<TSuat>5000%</TSuat>` hay một mã số thuế 200 ký tự thành
`DataError` của psycopg — thứ **không có** handler riêng, nên nó rơi vào lưới
`500 "lỗi không mong muốn"` và giấu mất chỗ hỏng. Cùng lối `_line` đã chặn mô
tả dòng: chặn ở đây thì thông điệp nói được **trường nào** quá khổ.

**`defusedxml` chứ không `xml.etree`.** Tệp ở đây do **người dùng tải lên**,
khác hẳn chuỗi mà chính máy chủ này sinh ra ở chiều gửi: thực thể ngoài (XXE)
đọc trộm tệp của máy chủ, và thực thể lồng nhau làm nổ bộ nhớ. Cả hai là đường
tấn công mà tài liệu stdlib nói thẳng là nó không chống.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import ParseError, fromstring

from ket.kernel.errors import InboundInvoiceXmlInvalidError
from ket.modules.einvoice.models import (
    INVOICE_FORM_MAX_LENGTH,
    INVOICE_NO_MAX_LENGTH,
    INVOICE_SERIAL_MAX_LENGTH,
    LINE_DESCRIPTION_MAX_LENGTH,
    LINE_UNIT_MAX_LENGTH,
    PARTY_ADDRESS_MAX_LENGTH,
    PARTY_NAME_MAX_LENGTH,
    TAX_AUTHORITY_CODE_MAX_LENGTH,
    TAX_CODE_MAX_LENGTH,
    VAT_RATE_TEXT_MAX_LENGTH,
)

_DATE_FORMATS: Final[tuple[str, ...]] = ("%Y-%m-%d", "%d/%m/%Y")
"""Hai dạng ngày gặp thật, không hơn.

`%Y-%m-%d` là dạng chuẩn khai trong QĐ1450; `%d/%m/%Y` là dạng vài nhà cung cấp
vẫn xuất. Dạng thứ ba không thêm: `%m/%d/%Y` **đọc được** đúng những chuỗi mà
`%d/%m/%Y` cũng đọc được, nên nhận cả hai là để ngày 03/04 lặng lẽ thành hai
ngày khác nhau tùy thứ tự thử."""

_VAT_EXEMPT_CODES: Final[frozenset[str]] = frozenset({"KCT", "KKKNT", "KHAC", "\\", "-"})
"""Giá trị `TSuat` **không phải một con số**.

`KCT` (không chịu thuế) và `KKKNT` (không kê khai nộp thuế) khác nhau về pháp lý
— quyền khấu trừ đầu vào khác nhau — nên cả hai giữ nguyên chuỗi gốc ở
`vat_rate_text` thay vì bị gộp. Đây chính là sự phân biệt mà chiều gửi phải
đánh mất ở biên giới EasyInvoice (xem `xml_builder`), và chiều nhận thì không
có lý do gì phải mất nó."""

MAX_LINE_NO: Final[int] = 32_767
"""Trần `smallint` của `inbound_einvoice_lines.line_no`."""

MONEY_INTEGER_DIGITS: Final[int] = 16
QUANTITY_INTEGER_DIGITS: Final[int] = 14
UNIT_PRICE_INTEGER_DIGITS: Final[int] = 18
RATE_INTEGER_DIGITS: Final[int] = 12
VAT_RATE_INTEGER_DIGITS: Final[int] = 3
"""Phần nguyên tối đa của từng loại số, lấy từ **chính hình dạng cột** sẽ nhận
nó: `numeric(18,2)` chừa 16 chữ số nguyên, `numeric(20,6)` chừa 14, và cứ thế.

Chặn ở đây chứ không để cột chặn hộ, vì cột chặn bằng `DataError` của psycopg —
thứ **không có** handler riêng (xem `api/middleware/problem_details`), nên nó
rơi vào lưới `500 "lỗi không mong muốn"` và giấu mất trường nào sai. Một
`<TSuat>5000%</TSuat>` là lỗi dữ liệu của tệp, và phải đọc ra như thế."""

CURRENCY_CODE_LENGTH: Final[int] = 3
_DEFAULT_CURRENCY: Final[str] = "VND"

INVOICE_NATURE_ORIGINAL: Final[int] = 1
INVOICE_NATURE_REPLACEMENT: Final[int] = 2
INVOICE_NATURE_ADJUSTMENT: Final[int] = 3
"""`TCHDon` — **tính chất** của tờ hóa đơn: gốc, thay thế, hay điều chỉnh.

Thẻ này là ranh giới giữa "một khoản mua" và "một lượt sửa khoản mua trước",
và bỏ qua nó là cách một tờ **điều chỉnh giảm** đi thẳng thành một chứng từ mua
làm **tăng** chi phí và thuế đầu vào đúng phần đáng lẽ phải giảm. Tờ hóa đơn ấy
tự nó nhất quán từng đồng — tổng khớp dòng, dòng khớp thuế — nên không phép
kiểm số học nào thấy; thứ sai nằm ở **dấu của nghiệp vụ**, thứ chỉ thẻ này nói.

Vắng mặt thì coi là hóa đơn gốc: phần lớn hóa đơn không mang khối `TTHDLQuan`
nào, và đó đúng là ý nghĩa của việc không mang."""

INVOICE_NATURE_RELATED_UNKNOWN: Final[int] = 9
"""Khối `TTHDLQuan` có mặt nhưng không khai `TCHDon`.

Một giá trị riêng chứ không đoán sang thay thế hay điều chỉnh: cái **biết chắc**
là tờ hóa đơn này nói về một tờ khác, còn nói theo kiểu nào thì tệp không nói,
và hai kiểu ấy ghi sổ ngược dấu nhau. Đoán ở đây là đúng thứ mà việc đọc
`TCHDon` sinh ra để tránh. Nó rơi vào cùng nhánh từ chối với hai tính chất kia,
nên nó không mở thêm đường nào — nó chỉ không nói dối trong thông điệp."""

_KNOWN_NATURES: Final[frozenset[int]] = frozenset(
    {INVOICE_NATURE_ORIGINAL, INVOICE_NATURE_REPLACEMENT, INVOICE_NATURE_ADJUSTMENT}
)


@dataclass(frozen=True, slots=True)
class InboundLineData:
    """Một dòng hàng hóa / dịch vụ đọc được từ `DSHHDVu/HHDVu`."""

    line_no: int
    description: str
    unit: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal
    """`ThTien` — tiền hàng **trước** thuế của dòng."""

    vat_rate: Decimal | None
    """Thuế suất theo **phần trăm** (`10`, `8`, `5`), `None` khi `TSuat` không
    phải một con số. Cùng đơn vị với `purchase_invoice_lines.vat_rate` —
    `kernel/money.VAT_RATE_SCALE` nói rõ nó ghi bằng phần trăm."""

    vat_rate_text: str | None
    """Chuỗi `TSuat` nguyên văn. Có mặt vì `vat_rate IS NULL` một mình không
    phân biệt được `KCT` với `KKKNT` — xem `_VAT_EXEMPT_CODES`."""


@dataclass(frozen=True, slots=True)
class VatGroupData:
    """Một dòng của bảng tổng hợp theo thuế suất (`TToan/THTTLTSuat/LTSuat`).

    Bảng này là chỗ **duy nhất** tờ hóa đơn khai tiền thuế: dòng hàng chỉ mang
    `ThTien` và `TSuat`, còn `TThue` được cộng theo **nhóm thuế suất**. Muốn có
    tiền thuế của từng dòng thì phải chia ngược từ đây — xem `inbound.py`.
    """

    vat_rate: Decimal | None
    taxable_amount: Decimal
    vat_amount: Decimal


@dataclass(frozen=True, slots=True)
class InboundInvoiceData:
    """Toàn bộ những gì đọc được từ một tờ hóa đơn đầu vào."""

    seller_tax_code: str
    seller_name: str
    seller_address: str | None
    buyer_tax_code: str
    buyer_name: str | None
    buyer_address: str | None

    invoice_form: str
    invoice_serial: str
    invoice_no: str
    invoice_date: date
    tax_authority_code: str | None

    currency_code: str
    exchange_rate: Decimal

    total_before_tax: Decimal
    total_vat: Decimal
    total_amount: Decimal

    nature: int
    """`TCHDon` — 1 gốc, 2 thay thế, 3 điều chỉnh. Xem `INVOICE_NATURE_ORIGINAL`."""

    related_form: str | None
    related_serial: str | None
    related_no: str | None
    related_date: date | None
    """Danh tính tờ hóa đơn **bị** thay thế hoặc điều chỉnh (`TTHDLQuan`).

    Bốn trường chứ một chuỗi hiển thị: người dùng phải tìm được tờ gốc trong sổ,
    và một chuỗi ghép sẵn thì không tra được. Rỗng ở hóa đơn gốc."""

    lines: tuple[InboundLineData, ...]
    vat_groups: tuple[VatGroupData, ...]


def parse_inbound_xml(content: bytes) -> InboundInvoiceData:
    """Đọc một tệp XML hóa đơn đầu vào, hoặc từ chối nó nêu rõ chỗ hỏng.

    Mọi đường thoát đều là `InboundInvoiceXmlInvalidError` (422): tệp đến từ
    lượt tải lên, nên "không đọc được" là lỗi dữ liệu gửi lên chứ không phải sự
    cố của máy chủ. Trả `500` ở đây sẽ giấu mất câu duy nhất giúp được người
    dùng — *thẻ nào thiếu*.
    """
    root = _root_of(content)
    seller = _require_child(root, "NBan")
    buyer = _require_child(root, "NMua")

    invoice_form = _require_bounded(root, "KHMSHDon", INVOICE_FORM_MAX_LENGTH)
    invoice_serial = _require_bounded(root, "KHHDon", INVOICE_SERIAL_MAX_LENGTH)
    invoice_no = _require_bounded(root, "SHDon", INVOICE_NO_MAX_LENGTH)
    related = _find(root, "TTHDLQuan")
    return InboundInvoiceData(
        seller_tax_code=_require_bounded(seller, "MST", TAX_CODE_MAX_LENGTH),
        seller_name=_require_bounded(seller, "Ten", PARTY_NAME_MAX_LENGTH),
        seller_address=_bounded(_text(seller, "DChi"), "DChi", PARTY_ADDRESS_MAX_LENGTH),
        buyer_tax_code=_require_bounded(buyer, "MST", TAX_CODE_MAX_LENGTH),
        buyer_name=_bounded(_text(buyer, "Ten"), "Ten", PARTY_NAME_MAX_LENGTH),
        buyer_address=_bounded(_text(buyer, "DChi"), "DChi", PARTY_ADDRESS_MAX_LENGTH),
        invoice_form=invoice_form,
        invoice_serial=invoice_serial,
        invoice_no=invoice_no,
        invoice_date=_require_date(root, "NLap"),
        tax_authority_code=_bounded(_text(root, "MCCQT"), "MCCQT", TAX_AUTHORITY_CODE_MAX_LENGTH),
        currency_code=_currency(root),
        exchange_rate=_number(_text(root, "TGia"), "TGia", RATE_INTEGER_DIGITS) or Decimal(1),
        total_before_tax=_require_number(root, "TgTCThue"),
        total_vat=_require_number(root, "TgTThue"),
        total_amount=_require_number(root, "TgTTTBSo"),
        nature=_nature(root, related),
        related_form=_related_text(related, "KHMSHDCLQuan", INVOICE_FORM_MAX_LENGTH),
        related_serial=_related_text(related, "KHHDCLQuan", INVOICE_SERIAL_MAX_LENGTH),
        related_no=_related_text(related, "SHDCLQuan", INVOICE_NO_MAX_LENGTH),
        related_date=_optional_date(related, "NLHDCLQuan") if related is not None else None,
        lines=_lines(root),
        vat_groups=_vat_groups(root),
    )


def _nature(root: Element, related: Element | None) -> int:
    """Tính chất tờ hóa đơn — xem `INVOICE_NATURE_ORIGINAL`.

    Đọc `TCHDon` ở **khối liên quan trước**, rồi mới tới phần chung: vài nhà
    cung cấp đặt nó ở một chỗ, vài nhà cung cấp đặt ở chỗ kia. Khối liên quan
    có mặt mà không khai tính chất thì nó **không** phải hóa đơn gốc — một tờ
    hóa đơn gốc không có gì để liên quan tới.

    Một giá trị ngoài ba giá trị đã biết là lỗi dữ liệu, không phải mặc định:
    đoán "chắc là gốc" ở đây đúng bằng việc bỏ qua thẻ này.
    """
    raw = _text(related, "TCHDon") if related is not None else None
    if raw is None:
        raw = _text(root, "TCHDon")
    if raw is None:
        return INVOICE_NATURE_RELATED_UNKNOWN if related is not None else INVOICE_NATURE_ORIGINAL
    try:
        nature = int(raw)
    except ValueError:
        raise InboundInvoiceXmlInvalidError(
            f"Tính chất hóa đơn không đọc được: {raw!r}", reason="TCHDon không phải số"
        ) from None
    if nature not in _KNOWN_NATURES:
        raise InboundInvoiceXmlInvalidError(
            f"Tính chất hóa đơn không thuộc ba giá trị đã biết: {raw!r}",
            reason="TCHDon lạ",
        )
    # `TCHDon` = 1 kèm một khối liên quan là hai lời khai ngược nhau trên cùng
    # một tệp. Tin vào `1` sẽ để tờ hóa đơn đi tiếp thành một chứng từ mua
    # thường, tức chọn đúng cách hiểu nguy hiểm hơn trong hai cách.
    if nature == INVOICE_NATURE_ORIGINAL and related is not None:
        return INVOICE_NATURE_RELATED_UNKNOWN
    return nature


def _related_text(related: Element | None, name: str, limit: int) -> str | None:
    return _bounded(_text(related, name), name, limit) if related is not None else None


def _optional_date(element: Element, name: str) -> date | None:
    return _require_date(element, name) if _text(element, name) is not None else None


def _root_of(content: bytes) -> Element:
    """Cây XML đã phân giải, với DTD và thực thể bị chặn ngay từ đầu.

    `forbid_dtd` chứ không chỉ `forbid_entities`: một tờ hóa đơn hợp lệ không
    khai DTD bao giờ, nên chặn cả khối là chặn luôn mọi biến thể bom thực thể
    mà không mất gì.
    """
    if not content:
        raise InboundInvoiceXmlInvalidError("Tệp hóa đơn rỗng", reason="tệp không có nội dung")
    try:
        parsed = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except ParseError as error:
        raise InboundInvoiceXmlInvalidError(
            "Tệp không phải XML đọc được", reason=str(error)
        ) from error
    except ValueError as error:
        # `defusedxml` ném `DTDForbidden` / `EntitiesForbidden` / `ExternalReferenceForbidden`,
        # cả ba kế thừa `ValueError`. Một tệp mang bom thực thể **không** phải
        # sự cố máy chủ: nó là tệp sai, và người tải lên cần biết vì sao.
        raise InboundInvoiceXmlInvalidError(
            "Tệp XML chứa khai báo bị cấm (DTD hoặc thực thể)", reason=str(error)
        ) from error
    if isinstance(parsed, Element):
        return parsed
    raise InboundInvoiceXmlInvalidError(  # pragma: no cover - `fromstring` luôn trả Element
        "Tệp không phải XML đọc được", reason="không dựng được cây phần tử"
    )


def _local_name(tag: object) -> str:
    """Tên thẻ **không kèm namespace**.

    Khớp theo tên cục bộ chứ không theo `{uri}tag`: cùng một tờ hóa đơn TCT được
    xuất ra khi thì trần, khi thì trong namespace riêng của nhà cung cấp, và một
    bộ đọc buộc đúng URI sẽ mù với nửa số tệp thật (bằng chứng: bản tích hợp ở
    `beta.konek.vn` dùng `local-name()` ở **mọi** biểu thức XPath của nó).
    """
    return str(tag).rpartition("}")[2]


def _descendants(element: Element, name: str) -> Iterator[Element]:
    for child in element.iter():
        if _local_name(child.tag) == name:
            yield child


def _find(element: Element, name: str) -> Element | None:
    return next(_descendants(element, name), None)


def _children(element: Element, name: str) -> Iterator[Element]:
    """Con **trực tiếp** mang tên này.

    Khác `_descendants`: dòng hàng phải là con trực tiếp của `DSHHDVu`, nếu
    không thì một thẻ `HHDVu` lồng trong phần chữ ký hay phần phụ lục nào đó sẽ
    thành một dòng hàng thứ n+1 không ai lập ra.
    """
    for child in element:
        if _local_name(child.tag) == name:
            yield child


def _require_child(element: Element, name: str) -> Element:
    found = _find(element, name)
    if found is None:
        raise InboundInvoiceXmlInvalidError(
            f"Tệp hóa đơn thiếu khối bắt buộc `{name}`", reason=f"thiếu {name}"
        )
    return found


def _text(element: Element, name: str) -> str | None:
    found = _find(element, name)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _bounded(value: str | None, name: str, limit: int) -> str | None:
    """Chuỗi đã chặn độ dài — xem docstring đầu tệp về vì sao chặn ở đây.

    Trần truyền vào từ hằng số của `models`, nên hai bên không lệch nhau được:
    nới cột mà quên nới chỗ này chỉ làm phép chặn rộng ra, còn thu cột mà quên
    thu chỗ này thì bài kiểm độ dài đỏ.
    """
    if value is not None and len(value) > limit:
        raise InboundInvoiceXmlInvalidError(
            f"Thẻ `{name}` dài quá {limit} ký tự", reason=f"{name} quá dài"
        )
    return value


def _require_bounded(element: Element, name: str, limit: int) -> str:
    value = _bounded(_require_text(element, name), name, limit)
    assert value is not None  # noqa: S101 — `_require_text` đã ném khi thiếu
    return value


def _require_text(element: Element, name: str) -> str:
    value = _text(element, name)
    if value is None:
        raise InboundInvoiceXmlInvalidError(
            f"Tệp hóa đơn thiếu thẻ bắt buộc `{name}`", reason=f"thiếu {name}"
        )
    return value


def _number(raw: str | None, name: str, digits: int = MONEY_INTEGER_DIGITS) -> Decimal | None:
    """Một con số thập phân **chuẩn**, hoặc lỗi — không bao giờ là một phỏng đoán.

    Không gỡ dấu phân nhóm, không đổi phẩy thành chấm: xem docstring đầu tệp về
    con số đã bị nhân một trăm lần ở bản tích hợp cũ. Một tờ hóa đơn viết
    `1.000.000,50` là một tờ hóa đơn sai chuẩn, và nói ra điều đó rẻ hơn nhiều
    so với ghi sổ một số tiền sai mà không ai biết.
    """
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise InboundInvoiceXmlInvalidError(
            f"Thẻ `{name}` không phải số thập phân chuẩn: {raw!r}",
            reason=f"{name} không đọc được thành số",
        ) from error
    if not value.is_finite():
        raise InboundInvoiceXmlInvalidError(
            f"Thẻ `{name}` không phải số hữu hạn: {raw!r}", reason=f"{name} không hữu hạn"
        )
    # `is_finite()` **không** đủ: `1E+999999999` là một `Decimal` hữu hạn hoàn
    # toàn hợp lệ, và nó làm `round_money` ném `InvalidOperation` — một `500`
    # trước cả lượt ghi nào — còn cột `numeric` thì tràn.
    if value.adjusted() >= digits:
        raise InboundInvoiceXmlInvalidError(
            f"Thẻ `{name}` mang con số quá lớn: {raw!r}", reason=f"{name} quá lớn"
        )
    return value


def _require_number(element: Element, name: str, digits: int = MONEY_INTEGER_DIGITS) -> Decimal:
    value = _number(_require_text(element, name), name, digits)
    if value is None:  # pragma: no cover - `_require_text` đã ném khi thiếu
        raise InboundInvoiceXmlInvalidError(
            f"Tệp hóa đơn thiếu thẻ bắt buộc `{name}`", reason=f"thiếu {name}"
        )
    return value


def _require_date(element: Element, name: str) -> date:
    raw = _require_text(element, name)
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, pattern).date()  # noqa: DTZ007 — ngày trên tờ hóa đơn, không mốc thời gian
        except ValueError:
            continue
    raise InboundInvoiceXmlInvalidError(
        f"Thẻ `{name}` không phải ngày đọc được: {raw!r}",
        reason=f"{name} không đọc được thành ngày",
    )


def _currency(root: Element) -> str:
    """Mã tiền tệ của hóa đơn; vắng mặt nghĩa là đồng Việt Nam.

    Mặc định chứ không bắt buộc: `DVTTe` là thẻ tùy chọn và phần lớn hóa đơn
    trong nước bỏ trống nó. Từ chối cả tờ hóa đơn vì thiếu một thẻ mà chính
    chuẩn cho phép thiếu là biến một mặc định thành một lỗi.
    """
    raw = _text(root, "DVTTe")
    if raw is None:
        return _DEFAULT_CURRENCY
    code = raw.strip().upper()
    if len(code) != CURRENCY_CODE_LENGTH or not code.isalpha():
        raise InboundInvoiceXmlInvalidError(
            f"Mã tiền tệ không hợp lệ: {raw!r}", reason="DVTTe không phải mã ISO 3 ký tự"
        )
    return code


def _vat_rate(raw: str | None) -> Decimal | None:
    """`TSuat` thành thuế suất phần trăm, hoặc `None` khi nó không phải số.

    Ba dạng gặp thật: `10%`, `10`, và một mã chữ (`KCT`, `KKKNT`). Dạng
    `KHAC:5%` — "thuế suất khác" của QĐ1450 — mang một con số **thật** sau dấu
    hai chấm, nên nó đi vào nhánh số chứ không nhánh mã chữ.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if ":" in text:
        text = text.partition(":")[2].strip()
    text = text.removesuffix("%").strip()
    if not text or text.upper() in _VAT_EXEMPT_CODES:
        return None
    try:
        rate = Decimal(text)
    except InvalidOperation:
        # Một mã chữ chưa từng gặp. Trả `None` chứ không ném: tờ hóa đơn vẫn
        # nạp được, và `vat_rate_text` giữ nguyên chuỗi để người dùng đọc — còn
        # tiền thuế thì lấy từ bảng tổng hợp theo thuế suất, không từ đây.
        return None
    if not rate.is_finite() or rate.adjusted() >= VAT_RATE_INTEGER_DIGITS:
        # Khác nhánh trên: đây **là** một con số, chỉ là con số không vào nổi
        # `numeric(5,2)`. Ném chứ không trả `None` — `None` nghĩa là "tờ hóa đơn
        # ghi một mã chữ", và nói dối điều đó sẽ giấu một thuế suất sai.
        raise InboundInvoiceXmlInvalidError(
            f"Thuế suất ngoài khoảng hợp lệ: {raw!r}", reason="TSuat quá lớn"
        )
    return rate


def _lines(root: Element) -> tuple[InboundLineData, ...]:
    container = _find(root, "DSHHDVu")
    if container is None:
        raise InboundInvoiceXmlInvalidError(
            "Tệp hóa đơn không có danh sách hàng hóa dịch vụ", reason="thiếu DSHHDVu"
        )
    lines = tuple(
        _line(element, fallback_no=index)
        for index, element in enumerate(_children(container, "HHDVu"), start=1)
        if not _is_note_line(element)
    )
    if not lines:
        raise InboundInvoiceXmlInvalidError(
            "Tệp hóa đơn không có dòng hàng hóa dịch vụ nào", reason="DSHHDVu rỗng"
        )
    return lines


_NOTE_LINE_NATURE: Final[str] = "4"
"""`TChat` = 4 — dòng **ghi chú**, không mang tiền.

Bỏ qua hẳn thay vì đọc rồi bỏ sau: dòng ghi chú thường không có `ThTien` nào,
nên để nó đi tiếp là từ chối cả tờ hóa đơn vì thiếu một thẻ mà chính dòng ấy
không có lý do mang. Ba tính chất còn lại (1 hàng hóa dịch vụ, 2 khuyến mại,
3 chiết khấu thương mại) **đều** mang tiền nên đều giữ lại — phần chiết khấu
làm tổng các dòng lệch tổng phải trả, và chỗ nói ra điều đó là phép kiểm tổng
lúc lập chứng từ, không phải một lượt bỏ dòng lặng lẽ ở đây."""


def _is_note_line(element: Element) -> bool:
    return (_text(element, "TChat") or "") == _NOTE_LINE_NATURE


def _line(element: Element, *, fallback_no: int) -> InboundLineData:
    """Một dòng hàng.

    **`DGia` vắng mặt thì để trống, không chia ngược từ `ThTien`.** Hóa đơn
    điện, nước và nhiều hóa đơn dịch vụ chỉ khai thành tiền, và một đơn giá suy
    ra bằng phép chia là con số **không có trên tờ hóa đơn**: nhân lại nó với số
    lượng thường không ra đúng `ThTien`, nên nó sẽ thành một mâu thuẫn nội bộ
    mà không ai giải thích được. `purchase_invoice_lines.unit_price_fc` cho
    phép `NULL` đúng vì ca này, và docstring của `PurchaseInvoiceLineIn` đã nói
    rõ số người dùng chốt là `amount_fc` chứ không phải cặp số lượng × đơn giá.
    """
    raw_rate = _bounded(_text(element, "TSuat"), "TSuat", VAT_RATE_TEXT_MAX_LENGTH)
    line_no = _number(_text(element, "STT"), "STT", QUANTITY_INTEGER_DIGITS)
    return InboundLineData(
        line_no=_line_no(line_no, fallback_no),
        description=_require_bounded(element, "THHDVu", LINE_DESCRIPTION_MAX_LENGTH),
        unit=_bounded(_text(element, "DVTinh"), "DVTinh", LINE_UNIT_MAX_LENGTH),
        quantity=_number(_text(element, "SLuong"), "SLuong", QUANTITY_INTEGER_DIGITS),
        unit_price=_number(_text(element, "DGia"), "DGia", UNIT_PRICE_INTEGER_DIGITS),
        amount=_require_number(element, "ThTien"),
        vat_rate=_vat_rate(raw_rate),
        vat_rate_text=raw_rate,
    )


def _line_no(declared: Decimal | None, fallback: int) -> int:
    """`STT` của dòng, hoặc vị trí của nó khi tờ hóa đơn không đánh số.

    Chặn trần `smallint` ở đây: `<STT>99999</STT>` là một số hợp lệ với Python
    và một `DataError` với PostgreSQL, tức một `500` cho một tệp chỉ đơn giản là
    đánh số lạ. Ngoài khoảng thì rơi về vị trí — thứ tự trên giấy vẫn giữ được,
    và không có gì mất."""
    if declared is None or declared <= 0 or declared > MAX_LINE_NO:
        return fallback
    return int(declared)


def _vat_groups(root: Element) -> tuple[VatGroupData, ...]:
    """Bảng tổng hợp theo thuế suất — rỗng khi tờ hóa đơn không khai.

    Rỗng **được phép**: hóa đơn toàn dòng không chịu thuế không có gì để tổng
    hợp. Hệ quả của rỗng nằm ở `inbound.py` (tiền thuế từng dòng lấy đâu ra),
    không ở đây — tệp này chỉ đọc thứ có trên giấy.
    """
    container = _find(root, "THTTLTSuat")
    if container is None:
        return ()
    return tuple(
        VatGroupData(
            vat_rate=_vat_rate(_text(element, "TSuat")),
            taxable_amount=_require_number(element, "ThTien"),
            vat_amount=_number(_text(element, "TThue"), "TThue") or Decimal(0),
        )
        for element in _children(container, "LTSuat")
    )
