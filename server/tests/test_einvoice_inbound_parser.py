"""Bộ phân giải XML hóa đơn đầu vào (lát 7F-2b, FR-EIV-040).

Không cần cơ sở dữ liệu: phân giải là một phép biến đổi thuần từ byte sang dữ
liệu, và tách nó ra khỏi bộ test `db` giữ cho những bài đắt nhất của lát này —
số tiền và định dạng — chạy trong mili giây.

Bốn nhóm bài, và mỗi nhóm ghim một thứ đã hỏng thật ở đâu đó:

* **Số.** Bản tích hợp đang chạy ở `~/code/beta.konek.vn` bỏ mọi dấu chấm rồi
  đổi phẩy thành chấm, tức đoán định dạng Việt Nam cho mọi con số — nên
  `4545455.50` qua nó thành `454545550`. Hai bài ở đây khóa cả hai chiều: dấu
  chấm thập phân giữ nguyên, còn một tờ hóa đơn viết `1.000.000,50` bị **từ
  chối** chứ không bị đoán.
* **Tệp thù địch.** Tệp đến từ lượt tải lên của người dùng, nên XXE và bom thực
  thể là đường tấn công thật, không phải giả định.
* **Hình dạng gặp ngoài đời.** Namespace lúc có lúc không, `TSuat` mang hậu tố
  `%`, hóa đơn điện/nước không có `DGia`, dòng ghi chú không mang tiền.
* **Sự thật pháp lý không được gộp.** `KCT` (không chịu thuế) và `KKKNT` (không
  kê khai nộp thuế) khác nhau về quyền khấu trừ đầu vào; chiều gửi buộc phải
  đánh mất sự phân biệt ấy ở biên giới nhà cung cấp, chiều nhận thì không.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from einvoice_support import inbound_lines_with, inbound_xml, related_invoice_block
from ket.kernel.errors import InboundInvoiceXmlInvalidError
from ket.modules.einvoice.inbound_parser import (
    INVOICE_NATURE_ADJUSTMENT,
    INVOICE_NATURE_ORIGINAL,
    INVOICE_NATURE_RELATED_UNKNOWN,
    INVOICE_NATURE_REPLACEMENT,
    parse_inbound_xml,
)

_ONE_LINE = """        <HHDVu>
          <STT>1</STT>
          <THHDVu>{name}</THHDVu>
          <ThTien>{amount}</ThTien>
          <TSuat>{rate}</TSuat>
        </HHDVu>"""

_NO_GROUPS = ""


def _single_line(*, name: str = "Dịch vụ", amount: str = "1000", rate: str = "10%") -> bytes:
    return inbound_xml(
        lines=_ONE_LINE.format(name=name, amount=amount, rate=rate),
        vat_groups=_NO_GROUPS,
        total_before_tax=amount,
        total_vat="0",
        total_amount=amount,
    )


def test_a_standard_invoice_reads_into_every_field_that_matters() -> None:
    """Một lượt đọc trọn tờ hóa đơn — bài neo cho mọi bài còn lại.

    Khẳng định **cả** phần đầu, phần dòng và bảng tổng hợp thuế suất trong cùng
    một bài: ba khối ấy đến từ ba nhánh khác nhau của bộ phân giải, và tách
    thành ba bài sẽ để lọt đúng lỗi hay gặp nhất — một khối đọc đúng trong khi
    khối bên cạnh trả về rỗng.
    """
    data = parse_inbound_xml(inbound_xml())

    assert data.seller_tax_code == "0101243150"
    assert data.buyer_tax_code == "0312345678"
    assert (data.invoice_form, data.invoice_serial, data.invoice_no) == ("1", "C26TAA", "00004994")
    assert data.invoice_date == date(2026, 3, 5)
    assert data.tax_authority_code == "M1-26-ABCDE-12345678"
    # `DVTTe` vắng mặt là ca thường gặp nhất của hóa đơn trong nước, và câu trả
    # lời đúng là một mặc định chứ không một lời từ chối.
    assert (data.currency_code, data.exchange_rate) == ("VND", Decimal(1))
    assert (data.total_before_tax, data.total_vat, data.total_amount) == (
        Decimal(2_000_000),
        Decimal(200_000),
        Decimal(2_200_000),
    )

    assert [line.description for line in data.lines] == ["Thép hộp 20x40", "Công vận chuyển"]
    assert [line.amount for line in data.lines] == [Decimal(1_200_000), Decimal(800_000)]
    assert {line.vat_rate for line in data.lines} == {Decimal(10)}
    assert data.vat_groups[0].vat_amount == Decimal(200_000)


def test_a_decimal_point_is_not_a_thousands_separator() -> None:
    """`4545455.50` phải ở nguyên giá trị của nó.

    Bài quan trọng nhất của tệp này. Bộ đọc số của bản tích hợp cũ gỡ mọi dấu
    chấm trước khi ép kiểu, nên con số này qua nó thành `454545550` — sai gấp
    một trăm lần, trên một số tiền, không một tiếng động nào.
    """
    data = parse_inbound_xml(_single_line(amount="4545455.50"))

    assert data.lines[0].amount == Decimal("4545455.50")
    assert data.total_amount == Decimal("4545455.50")


def test_a_vietnamese_grouped_number_is_refused_rather_than_guessed() -> None:
    """`1.000.000,50` là một tờ hóa đơn sai chuẩn, và nói ra rẻ hơn đoán.

    Đối trọng của bài trên: nếu một ngày ai đó "sửa" cho bộ đọc nhận luôn định
    dạng Việt Nam, bài kia vẫn xanh (dấu chấm thập phân vẫn ra đúng) còn bài
    này thì đỏ — vì lúc ấy `1.000.000,50` sẽ được đọc thành một con số thay vì
    bị từ chối, và cùng một chuỗi lại có hai cách hiểu.
    """
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(_single_line(amount="1.000.000,50"))

    assert "1.000.000,50" in str(error.value)


def test_the_same_invoice_reads_the_same_inside_a_provider_namespace() -> None:
    """Namespace lúc có lúc không — khớp theo **tên cục bộ** là thứ giữ cho cả
    hai đọc ra một kết quả. Một bộ đọc buộc đúng URI sẽ mù với nửa số tệp thật."""
    bare = parse_inbound_xml(inbound_xml())
    namespaced = parse_inbound_xml(
        inbound_xml(namespace="http://laphoadon.gdt.gov.vn/2014/09/invoicexml/v1")
    )

    assert namespaced == bare


def test_a_document_type_declaration_is_refused() -> None:
    """Bom thực thể vào bằng cửa DTD, nên cửa DTD đóng hẳn.

    Một tờ hóa đơn hợp lệ không khai DTD bao giờ, nên chặn cả khối không mất gì
    — và nó chặn luôn mọi biến thể của cùng một đòn.
    """
    bomb = (
        '<!DOCTYPE HDon [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;">]>\n'
    )
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(inbound_xml(prologue=bomb))

    assert "bị cấm" in str(error.value)


def test_an_external_entity_cannot_read_a_file_off_the_server() -> None:
    """XXE: tệp của người dùng không được biến thành một lượt đọc `/etc/passwd`."""
    xxe = '<!DOCTYPE HDon [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
    with pytest.raises(InboundInvoiceXmlInvalidError):
        parse_inbound_xml(inbound_xml(prologue=xxe, seller_name="&xxe;"))


def test_the_tax_rate_keeps_the_difference_between_kct_and_kkknt() -> None:
    """Hai mã chữ cùng cho `vat_rate` rỗng, nhưng **không** cùng một dòng dữ liệu.

    Quyền khấu trừ thuế đầu vào của hai loại khác nhau, nên gộp chúng ở đây là
    xóa một sự thật pháp lý ngay tại cửa nhận. `vat_rate_text` là chỗ giữ nó.
    """
    exempt = parse_inbound_xml(_single_line(rate="KCT")).lines[0]
    not_declared = parse_inbound_xml(_single_line(rate="KKKNT")).lines[0]

    assert exempt.vat_rate is None and not_declared.vat_rate is None
    assert (exempt.vat_rate_text, not_declared.vat_rate_text) == ("KCT", "KKKNT")


def test_an_other_rate_carries_a_real_number_after_the_colon() -> None:
    """`KHAC:5%` — "thuế suất khác" của QĐ1450 — là một con số thật, không một mã chữ."""
    assert parse_inbound_xml(_single_line(rate="KHAC:5%")).lines[0].vat_rate == Decimal(5)


def test_a_bare_rate_without_the_percent_sign_reads_the_same() -> None:
    assert parse_inbound_xml(_single_line(rate="8")).lines[0].vat_rate == Decimal(8)


def test_a_utility_bill_without_a_unit_price_leaves_it_empty() -> None:
    """Hóa đơn điện/nước chỉ khai thành tiền.

    Đơn giá để **trống** chứ không chia ngược từ `ThTien`: một con số suy ra
    bằng phép chia không có trên tờ hóa đơn, và nhân lại nó với số lượng thường
    không ra đúng thành tiền — tức một mâu thuẫn nội bộ không ai giải thích nổi.
    """
    line = parse_inbound_xml(_single_line(amount="1234567")).lines[0]

    assert line.unit_price is None
    assert line.amount == Decimal(1_234_567)


def test_a_note_line_carries_no_money_and_is_left_out() -> None:
    """`TChat` = 4 là dòng ghi chú — nó thường không có `ThTien` nào.

    Để nó đi tiếp là từ chối cả tờ hóa đơn vì thiếu một thẻ mà chính dòng ấy
    không có lý do mang.
    """
    note = """        <HHDVu>
          <TChat>4</TChat>
          <STT>3</STT>
          <THHDVu>Ghi chú: giao hàng trong giờ hành chính</THHDVu>
        </HHDVu>"""
    data = parse_inbound_xml(inbound_xml(lines=inbound_lines_with(note)))

    assert [line.line_no for line in data.lines] == [1, 2]


def test_a_promotional_line_stays_because_it_is_still_a_line() -> None:
    """`TChat` = 2 (khuyến mại) khai giá 0 nhưng **có** `ThTien`, nên nó ở lại.

    Chỉ dòng ghi chú bị bỏ. Bỏ thêm dòng khuyến mại sẽ là bộ phân giải tự quyết
    định thứ gì trên tờ hóa đơn đáng kể tới — và quyết định ấy thuộc về lượt
    lập chứng từ, nơi nó nhìn thấy được bằng phép kiểm tổng.
    """
    promo = """        <HHDVu>
          <TChat>2</TChat>
          <STT>3</STT>
          <THHDVu>Hàng khuyến mại</THHDVu>
          <ThTien>0</ThTien>
          <TSuat>10%</TSuat>
        </HHDVu>"""
    data = parse_inbound_xml(inbound_xml(lines=inbound_lines_with(promo)))

    assert [line.line_no for line in data.lines] == [1, 2, 3]
    assert data.lines[2].amount == Decimal(0)


@pytest.mark.parametrize("tag", ["SHDon", "KHHDon", "KHMSHDon", "NLap", "TgTThue"])
def test_a_missing_mandatory_tag_names_the_tag(tag: str) -> None:
    """Thông điệp phải nói **thẻ nào** thiếu.

    Đó là câu duy nhất giúp được người tải lên, và là lý do mọi đường thoát của
    bộ phân giải là `422` chứ không `500`. Duyệt năm thẻ cùng lúc chứ không một:
    lỗi hay gặp của một bộ kiểm bắt buộc là nó chỉ canh thẻ đầu tiên.
    """
    stripped = (
        inbound_xml().replace(f"<{tag}>".encode(), b"<Bo>").replace(f"</{tag}>".encode(), b"</Bo>")
    )
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(stripped)

    assert tag in str(error.value)


@pytest.mark.parametrize(
    ("written", "expected"),
    [("2026-03-05", date(2026, 3, 5)), ("05/03/2026", date(2026, 3, 5))],
)
def test_both_date_shapes_seen_in_the_wild_are_read(written: str, expected: date) -> None:
    """Dạng chuẩn QĐ1450 và dạng vài nhà cung cấp vẫn xuất — hai dạng, không hơn.

    Dạng thứ ba (`%m/%d/%Y`) cố ý không nhận: nó đọc được đúng những chuỗi mà
    `%d/%m/%Y` cũng đọc được, nên nhận cả hai là để ngày 03/04 lặng lẽ thành
    hai ngày khác nhau tùy thứ tự thử.
    """
    assert parse_inbound_xml(inbound_xml(invoice_date=written)).invoice_date == expected


def test_a_date_in_an_ambiguous_third_shape_is_refused() -> None:
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(inbound_xml(invoice_date="March 5, 2026"))

    assert "NLap" in str(error.value)


def test_an_empty_upload_is_a_data_error_not_a_crash() -> None:
    with pytest.raises(InboundInvoiceXmlInvalidError):
        parse_inbound_xml(b"")


def test_a_file_that_is_not_xml_at_all_says_so() -> None:
    with pytest.raises(InboundInvoiceXmlInvalidError):
        parse_inbound_xml(b"%PDF-1.7 khong phai xml")


def test_a_foreign_currency_invoice_keeps_its_rate() -> None:
    """Số tiền giữ **nguyên tệ** kèm tỷ giá, không quy đổi tại cửa nhận.

    Quy đổi ở đây rồi để chứng từ nhân ngược lại là hai lần làm tròn, và hai
    lần làm tròn là hai con số — cùng lập luận đã ghi ở `SubledgerEntry`.
    """
    data = parse_inbound_xml(
        inbound_xml(currency="USD").replace(
            b"<DVTTe>USD</DVTTe>", b"<DVTTe>USD</DVTTe><TGia>25400</TGia>"
        )
    )

    assert data.currency_code == "USD"
    assert data.exchange_rate == Decimal(25_400)


def test_a_currency_code_that_is_not_three_letters_is_refused() -> None:
    with pytest.raises(InboundInvoiceXmlInvalidError):
        parse_inbound_xml(inbound_xml(currency="DONG"))


def _with_tag_value(document: bytes, tag: str, value: str) -> bytes:
    """Thay nội dung lần xuất hiện **đầu tiên** của một thẻ, giữ nguyên phần còn lại."""
    pattern = re.compile(rf"<{tag}>.*?</{tag}>".encode(), re.DOTALL)
    replaced, count = pattern.subn(f"<{tag}>{value}</{tag}>".encode(), document, count=1)
    assert count == 1, f"không tìm thấy thẻ {tag} trong tệp mẫu"
    return replaced


def test_an_original_invoice_says_so_and_points_at_nothing() -> None:
    data = parse_inbound_xml(inbound_xml())

    assert data.nature == INVOICE_NATURE_ORIGINAL
    assert (data.related_form, data.related_serial, data.related_no, data.related_date) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("3", INVOICE_NATURE_ADJUSTMENT),
        ("2", INVOICE_NATURE_REPLACEMENT),
        # Khối liên quan có mặt mà `TCHDon` thì không: biết chắc tờ này nói về
        # một tờ khác, không biết nói kiểu gì — và hai kiểu ghi sổ ngược dấu.
        (None, INVOICE_NATURE_RELATED_UNKNOWN),
        # `TCHDon` = 1 **kèm** một khối liên quan là hai lời khai ngược nhau
        # trên cùng một tệp. Tin vào `1` sẽ để tờ hóa đơn đi tiếp thành một
        # chứng từ mua thường — đúng cách hiểu nguy hiểm hơn trong hai cách.
        ("1", INVOICE_NATURE_RELATED_UNKNOWN),
    ],
)
def test_an_invoice_about_another_invoice_never_reads_as_an_original(
    declared: str | None, expected: int
) -> None:
    """Bốn cách một tờ hóa đơn nói "tôi sửa một tờ khác", một câu trả lời chung.

    Đây là điều kiện mà bản đầu của lát **thiếu**: một tờ điều chỉnh giảm tự nó
    nhất quán từng đồng, nên nó lọt mọi phép kiểm số học rồi thành một chứng từ
    mua làm **tăng** chi phí và thuế đầu vào đúng phần đáng lẽ phải giảm.
    """
    data = parse_inbound_xml(inbound_xml(related=related_invoice_block(nature=declared)))

    assert data.nature == expected
    assert data.nature != INVOICE_NATURE_ORIGINAL
    assert (data.related_serial, data.related_no) == ("C26TAA", "00001111")
    assert data.related_date == date(2026, 2, 1)


def test_an_unknown_invoice_nature_is_refused_rather_than_assumed_original() -> None:
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(inbound_xml(related=related_invoice_block(nature="7")))

    assert "TCHDon" in str(error.value.details["reason"])


@pytest.mark.parametrize(
    ("tag", "length"),
    [("MST", 21), ("Ten", 401), ("SHDon", 51), ("KHHDon", 21), ("MCCQT", 101)],
)
def test_a_value_too_long_for_its_column_is_named_at_the_door(tag: str, length: int) -> None:
    """Chặn độ dài **ở đây**, không để cột `varchar` chặn hộ.

    Cột chặn bằng `DataError` của psycopg — thứ không có handler riêng, nên nó
    rơi vào lưới `500 "lỗi không mong muốn"` và giấu mất trường nào quá khổ.
    """
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(_with_tag_value(inbound_xml(), tag, "X" * length))

    assert tag in str(error.value)


def test_an_amount_that_cannot_be_money_is_refused_before_it_can_crash_rounding() -> None:
    """`1E+999999999` là một `Decimal` hữu hạn hoàn toàn hợp lệ.

    Nó làm `round_money` ném `InvalidOperation` — một `500` trước cả lượt ghi
    nào — còn cột `numeric` thì tràn. `is_finite()` một mình không thấy nó.
    """
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(_single_line(amount="1E+999999999"))

    assert "quá lớn" in str(error.value.details["reason"])


def test_a_tax_rate_that_cannot_fit_the_column_is_a_data_error_not_a_crash() -> None:
    """`5000%` **là** một con số, chỉ là con số không vào nổi `numeric(5,2)`.

    Ném chứ không trả `None`: `None` nghĩa là "tờ hóa đơn ghi một mã chữ", và
    nói dối điều đó sẽ giấu một thuế suất sai.
    """
    with pytest.raises(InboundInvoiceXmlInvalidError) as error:
        parse_inbound_xml(_single_line(rate="5000%"))

    assert "TSuat" in str(error.value.details["reason"])


def test_a_line_number_outside_smallint_falls_back_to_its_position() -> None:
    """`<STT>99999</STT>` hợp lệ với Python và là một `DataError` với PostgreSQL.

    Rơi về vị trí chứ không từ chối cả tờ hóa đơn: thứ tự trên giấy vẫn giữ
    được, và một cách đánh số lạ không phải lý do để mất một khoản mua.
    """
    odd = _single_line().replace(b"<STT>1</STT>", b"<STT>99999</STT>")

    assert parse_inbound_xml(odd).lines[0].line_no == 1
