"""Mảnh không-DB của lát 6E-2: đọc số thành chữ, hợp đồng `PrintTable`, và
render năm mẫu in mới (01-TT, 02-TT, ủy nhiệm chi, giấy báo có, 08a-TT).

Kiểm ở đây là kiểm **tờ giấy**: đúng số hiệu mẫu, đúng nhãn theo thông tư,
đúng khối chữ ký, và — quan trọng nhất — mẫu KHÔNG mang số hiệu thông tư khi
thông tư không có biểu mẫu đó. Dữ liệu thật đi qua HTTP kiểm ở
`test_voucher_print_api.py`.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from pypdf import PdfReader

from ket.kernel.config.printing.context import (
    DocumentPrintDetails,
    PrintField,
    PrintTable,
    PrintTableColumn,
)
from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.config.printing.seed import load_builtin_print_templates
from ket.kernel.money_words import amount_in_words
from ket.reporting.printing.template_service import (
    DocumentPrintContext,
    VoucherPrintLine,
    render_document_pdf,
)
from ket.reporting.rendering.header import UnitInfo


class TestAmountInWords:
    """01-TT/02-TT bắt buộc dòng "(Viết bằng chữ)" — chữ là thứ chặn sửa số
    trên tờ phiếu đã ký, nên sai một từ là sai chứng từ."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", "Không đồng chẵn"),
            ("15", "Mười lăm đồng chẵn"),
            ("21", "Hai mươi mốt đồng chẵn"),
            ("25", "Hai mươi lăm đồng chẵn"),
            ("101", "Một trăm lẻ một đồng chẵn"),
            ("150", "Một trăm năm mươi đồng chẵn"),
            ("5200000", "Năm triệu hai trăm nghìn đồng chẵn"),
            ("1000000000000", "Một nghìn tỷ đồng chẵn"),
        ],
    )
    def test_reads_the_common_shapes(self, value: str, expected: str) -> None:
        assert amount_in_words(Decimal(value)) == expected

    def test_zero_groups_keep_the_hundreds_that_tell_them_apart(self) -> None:
        """Nhóm ba chữ số ĐỨNG SAU phải đọc cả hàng trăm rỗng: bỏ đi thì
        1.000.005 và 1.000.500 đọc giống hệt nhau, và hai số đó lệch nhau
        495 nghìn."""
        assert amount_in_words(Decimal(1_000_005)) == "Một triệu không trăm lẻ năm đồng chẵn"
        assert amount_in_words(Decimal(1_000_500)) == "Một triệu năm trăm đồng chẵn"

    def test_fraction_is_read_not_dropped(self) -> None:
        """Không làm tròn cho câu chữ đẹp: phần lẻ có thì đọc ra. Số 0 đứng
        đầu đọc rời ("không năm"), số 0 ở cuối bỏ vì không đổi giá trị."""
        assert amount_in_words(Decimal("1234.05")).endswith("phẩy không năm đồng")
        assert amount_in_words(Decimal("1234.50")).endswith("phẩy năm đồng")
        # "chẵn" chỉ dành cho số nguyên — nói "chẵn" cho một số lẻ là nói sai.
        assert "chẵn" not in amount_in_words(Decimal("1234.05"))

    def test_currency_unit_and_negative(self) -> None:
        assert amount_in_words(Decimal(1200), unit="USD") == "Một nghìn hai trăm USD chẵn"
        assert amount_in_words(Decimal(-500)) == "Âm năm trăm đồng chẵn"


class TestPrintTableContract:
    def test_ragged_rows_die_at_construction(self) -> None:
        """Lệch số ô là bản in lệch cột — bắt lúc dựng chứ không để người đọc
        phát hiện qua một cột tiền nằm dưới tiêu đề "Số lượng"."""
        columns = (PrintTableColumn("A"), PrintTableColumn("B"))
        with pytest.raises(ValueError, match="cột"):
            PrintTable(columns=columns, rows=(("1", "2"), ("3",)))
        with pytest.raises(ValueError, match="Dòng cộng"):
            PrintTable(columns=columns, rows=(("1", "2"),), total_row=("x",))


def _template(code: str) -> PrintTemplate:
    entry, html = next(item for item in load_builtin_print_templates() if item[0].code == code)
    return PrintTemplate(
        document_type=entry.document_type,
        code=entry.code,
        name=entry.name,
        html_template=html,
        css_extra=None,
        is_default=entry.is_default,
        is_builtin=True,
    )


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def _context(
    *,
    title: str,
    details: DocumentPrintDetails,
    line_count: int = 2,
) -> DocumentPrintContext:
    lines = tuple(
        VoucherPrintLine(
            line_no=index + 1,
            account_code="1111" if index == 0 else "131",
            description="thu tiền khách",
            debit="5.200.000" if index == 0 else "",
            credit="" if index == 0 else "5.200.000",
        )
        for index in range(line_count)
    )
    return DocumentPrintContext(
        title=title,
        voucher_no="PT26-00007",
        document_date="10/02/2026",
        posting_date="10/02/2026",
        description="thu tiền hàng",
        draft=False,
        copy_line=None,
        lines=lines,
        total_debit="5.200.000",
        total_credit="5.200.000",
        signature_date_line="Ngày 10 tháng 02 năm 2026",
        unit=UnitInfo(name="Công ty thử", tax_code="0100000000", address="Hà Nội"),
        details=details,
    )


_CASH_DETAILS = DocumentPrintDetails(
    header_fields=(PrintField("Nợ", "1111"), PrintField("Có", "131")),
    fields=(
        PrintField("Họ và tên người nộp tiền", "Trần Thị Bích"),
        PrintField("Địa chỉ", "12 Lê Lợi, Đà Nẵng"),
        PrintField("Lý do nộp", "thu tiền hàng tháng 1"),
        PrintField("Kèm theo", "2 chứng từ gốc"),
    ),
    amount="5.200.000 VND",
    amount_in_words="Năm triệu hai trăm nghìn đồng chẵn",
)


class TestCashVoucherForms:
    def test_receipt_carries_form_01tt_labels_and_signatures(self) -> None:
        text = _pdf_text(
            render_document_pdf(
                _template("PHIEU-THU-01TT"), _context(title="Phiếu thu", details=_CASH_DETAILS)
            )
        )
        assert "Mẫu số 01 - TT" in text
        assert "99/2025/TT-BTC" in text
        assert "Họ và tên người nộp tiền" in text
        assert "Trần Thị Bích" in text
        assert "2 chứng từ gốc" in text
        assert "Năm triệu hai trăm nghìn đồng chẵn" in text
        assert "Đã nhận đủ số tiền" in text
        for role in ("Giám đốc", "Kế toán trưởng", "Người nộp tiền", "Người lập phiếu", "Thủ quỹ"):
            assert role in text

    def test_payment_is_form_02tt_and_names_the_receiver(self) -> None:
        details = DocumentPrintDetails(
            header_fields=_CASH_DETAILS.header_fields,
            fields=(
                PrintField("Họ và tên người nhận tiền", "Nguyễn Văn An"),
                PrintField("Địa chỉ", "Phòng Hành chính"),
                PrintField("Lý do chi", "tạm ứng công tác"),
            ),
            amount="1.500.000 VND",
            amount_in_words="Một triệu năm trăm nghìn đồng chẵn",
        )
        text = _pdf_text(
            render_document_pdf(
                _template("PHIEU-CHI-02TT"), _context(title="Phiếu chi", details=details)
            )
        )
        assert "Mẫu số 02 - TT" in text
        assert "Họ và tên người nhận tiền" in text
        assert "Người nhận tiền" in text
        assert "Mẫu số 01 - TT" not in text

    def test_detail_table_appears_only_when_the_voucher_has_more_than_one_pair(self) -> None:
        """Tờ 01-TT chỉ có MỘT cặp Nợ/Có; phần mềm cho nhiều dòng nên bảng chi
        tiết chỉ xuất hiện khi thật sự có nhiều hơn một cặp — in một bảng hai
        dòng lặp lại đúng khối "Nợ/Có" góc phải là bày thêm giấy."""
        simple = _pdf_text(
            render_document_pdf(
                _template("PHIEU-THU-01TT"), _context(title="Phiếu thu", details=_CASH_DETAILS)
            )
        )
        assert "Cộng" not in simple
        detailed = _pdf_text(
            render_document_pdf(
                _template("PHIEU-THU-01TT"),
                _context(title="Phiếu thu", details=_CASH_DETAILS, line_count=4),
            )
        )
        assert "Cộng" in detailed

    def test_foreign_currency_notes_reach_the_footer(self) -> None:
        details = DocumentPrintDetails(
            fields=_CASH_DETAILS.fields,
            amount="200,00 USD",
            amount_in_words="Hai trăm USD chẵn",
            notes=(
                PrintField("Tỷ giá ngoại tệ", "25.400,0000"),
                PrintField("Số tiền quy đổi", "5.080.000"),
            ),
        )
        text = _pdf_text(
            render_document_pdf(
                _template("PHIEU-THU-01TT"), _context(title="Phiếu thu", details=details)
            )
        )
        assert "Tỷ giá ngoại tệ" in text
        assert "5.080.000" in text


class TestBankVoucherForms:
    """Ủy nhiệm chi và giấy báo có KHÔNG có biểu mẫu trong TT99 Phụ lục I."""

    _DETAILS = DocumentPrintDetails(
        header_fields=(PrintField("Nợ", "331"), PrintField("Có", "1121")),
        fields=(
            PrintField("Đơn vị trả tiền", "Công ty thử"),
            PrintField("Số tài khoản", "0011001234567"),
            PrintField("Tại ngân hàng", "Vietcombank — CN Hà Nội"),
            PrintField("Đơn vị thụ hưởng", "Công ty TNHH Vật tư Sao Mai"),
            PrintField("Số tài khoản thụ hưởng", "19001199887766"),
            PrintField("Tại ngân hàng", "Techcombank"),
            PrintField("Nội dung", "thanh toán hóa đơn 0001234"),
        ),
        amount="88.000.000 VND",
        amount_in_words="Tám mươi tám triệu đồng chẵn",
    )

    def test_payment_order_shows_the_beneficiary_account_and_claims_no_form_code(self) -> None:
        text = _pdf_text(
            render_document_pdf(
                _template("UY-NHIEM-CHI"), _context(title="Ủy nhiệm chi", details=self._DETAILS)
            )
        )
        assert "19001199887766" in text  # FR-SYS-033
        assert "Chủ tài khoản" in text
        # Thông tư không có mẫu ủy nhiệm chi — in "Mẫu số …" là nói dối nguồn gốc.
        assert "Mẫu số" not in text
        assert "TT-BTC" not in text

    def test_credit_advice_says_it_is_an_internal_copy(self) -> None:
        text = _pdf_text(
            render_document_pdf(
                _template("GIAY-BAO-CO"), _context(title="Giấy báo có", details=self._DETAILS)
            )
        )
        assert "Bản in nội bộ" in text
        assert "Mẫu số" not in text


class TestCountSheetForm:
    def test_form_08att_layout_with_denominations(self) -> None:
        details = DocumentPrintDetails(
            fields=(
                PrintField("Tài khoản quỹ", "1111 - Tiền Việt Nam"),
                PrintField("Thời điểm kiểm kê", "31/01/2026"),
            ),
            tables=(
                PrintTable(
                    columns=(
                        PrintTableColumn("STT", align="center", width="8%"),
                        PrintTableColumn("Diễn giải"),
                        PrintTableColumn("Số lượng (tờ)", align="money", width="18%"),
                        PrintTableColumn("Số tiền", align="money", width="22%"),
                    ),
                    rows=(
                        ("I", "Số dư theo sổ quỹ", "x", "500.000"),
                        ("II", "Số kiểm kê thực tế", "x", "480.000"),
                        ("1", "Trong đó: - Loại 100.000", "4", "400.000"),
                        ("2", "- Loại 20.000", "4", "80.000"),
                        ("III", "Chênh lệch (III = I - II)", "x", "20.000"),
                    ),
                ),
            ),
            notes=(
                PrintField("Lý do thiếu", ""),
                PrintField("Kết luận sau khi kiểm kê quỹ", "chờ xử lý"),
            ),
        )
        context = _context(title="Bảng kiểm kê quỹ", details=details, line_count=0)
        text = _pdf_text(render_document_pdf(_template("BIEN-BAN-KIEM-KE-QUY-08aTT"), context))
        assert "Mẫu số 08a - TT" in text
        assert "Dùng cho VNĐ" in text
        assert "Số dư theo sổ quỹ" in text
        assert "Chênh lệch (III = I - II)" in text
        assert "Loại 100.000" in text
        assert "Người chịu trách nhiệm kiểm kê quỹ" in text
        assert "đại diện thủ quỹ" in text
        # Biên bản không phải chứng từ: không có dòng định khoản nào để in.
        assert "Tài khoản" in text and "Cộng" not in text


class TestBuiltinManifest:
    def test_every_new_form_is_registered_as_the_default_of_its_kind(self) -> None:
        by_key = {
            (entry.document_type, entry.code): entry
            for entry, _html in load_builtin_print_templates()
        }
        expected = {
            ("GLE", "PHIEU-KE-TOAN"),
            ("PT", "PHIEU-THU-01TT"),
            ("PC", "PHIEU-CHI-02TT"),
            ("UNC", "UY-NHIEM-CHI"),
            ("BC", "GIAY-BAO-CO"),
            # Séc và chuyển tiền nội bộ (review 6E-2, M-2): hai loại còn lại của
            # phân hệ ngân hàng có quyền `.print`, nên thiếu mẫu là nút In luôn
            # 404 và nhánh nhãn của chúng thành mã không đường tới.
            ("SEC", "SEC-CHUYEN-KHOAN"),
            ("CTNB", "CHUYEN-TIEN-NOI-BO"),
            ("KKQ", "BIEN-BAN-KIEM-KE-QUY-08aTT"),
        }
        assert set(by_key) == expected
        assert all(entry.is_default for entry in by_key.values())

    def test_templates_are_scheme_neutral(self) -> None:
        """Không mẫu nào thuộc gói cấu hình: hình dạng 01-TT/02-TT/08a-TT không
        phụ thuộc hệ thống tài khoản, nên TT99 và TT133 dùng chung một tờ.
        Ngày có mẫu khác nhau giữa hai chế độ thì nó vào gói, không vào đây."""
        for _entry, html in load_builtin_print_templates():
            assert "TT133" not in html
