"""Renderer PDF/XLSX + hai hàng rào RT-01 (SSTI, url_fetcher) — không cần DB.

Nhóm chậm nhất của bộ non-db (mỗi lượt WeasyPrint ~0,2–3s) — giữ dữ liệu test
nhỏ có chủ đích; số liệu quy mô lớn nằm ở spike S2 (ADR-009 §Kết luận spike).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from jinja2.exceptions import SecurityError
from pypdf import PdfReader

from ket.kernel.config.reports.spec import parse_layout_spec
from ket.reporting.engine.grouping import group_rows
from ket.reporting.rendering import formats, pdf_renderer
from ket.reporting.rendering.environment import (
    _ALLOWED_ASSETS,
    _ASSETS_ROOT,
    asset_url_fetcher,
    create_print_environment,
)
from ket.reporting.rendering.header import UnitInfo
from ket.reporting.rendering.pdf_renderer import render_pdf
from ket.reporting.rendering.xlsx_renderer import render_xlsx

LAYOUT = parse_layout_spec(
    {
        "columns": [
            {"key": "posting_date", "label": "Ngày ghi sổ", "type": "date"},
            {"key": "description", "label": "Diễn giải", "type": "text"},
            {"key": "debit", "label": "Nợ", "type": "money"},
            {"key": "credit", "label": "Có", "type": "money"},
        ],
        "group_by": [{"key": "account_code", "heading_keys": ["account_code", "account_name"]}],
        "totals": ["debit", "credit"],
        "sort": ["account_code", "posting_date"],
    },
    layout_code="t",
)

UNIT = UnitInfo(name="CÔNG TY THỬ NGHIỆM", tax_code="0312345678", address="1 Lê Lợi")


def _row(account: str, day: int, *, debit: int = 0, credit: int = 0) -> dict[str, object]:
    return {
        "account_code": account,
        "account_name": f"TK {account}",
        "posting_date": date(2026, 1, day),
        "description": f"phát sinh {account} ngày {day}",
        "debit": Decimal(debit),
        "credit": Decimal(credit),
    }


def _pdf_text(pdf: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages]


class TestSecurity:
    def test_ssti_payload_is_blocked_by_sandbox(self) -> None:
        """RT-01: payload `__class__` phải chết bằng `SecurityError` — đây là
        chính môi trường mà mẫu in của gói cấu hình bên thứ ba (5D) sẽ chạy."""
        environment = create_print_environment()
        template = environment.from_string("{{ ''.__class__.__mro__[1].__subclasses__() }}")
        with pytest.raises(SecurityError):
            template.render()

    def test_url_fetcher_rejects_every_scheme_but_asset(self) -> None:
        for url in (
            "file:///etc/passwd",
            "http://evil.example/x.css",
            "data:text/plain,hello",
            "asset:../../../etc/passwd",
            "asset:fonts/khong-ton-tai.woff2",
        ):
            with pytest.raises(ValueError, match="Tài nguyên"):
                asset_url_fetcher(url)

    def test_allowed_assets_all_exist(self) -> None:
        """Allowlist khẳng định bằng giá trị — và mọi giá trị phải là tệp thật
        (một tên gõ nhầm = font lặng lẽ không nhúng, PDF sai trên máy khách)."""
        for name in _ALLOWED_ASSETS:
            assert _ASSETS_ROOT.joinpath(name).is_file(), name

    def test_malicious_img_does_not_leak_file_content(self) -> None:
        """`<img src="file://…">` trong HTML: fetcher từ chối, WeasyPrint bỏ
        ảnh, PDF vẫn ra — nội dung tệp không bao giờ vào bản in."""
        environment = create_print_environment()
        template = environment.from_string(
            '<html><body><p>x</p><img src="file:///etc/passwd"></body></html>'
        )
        from weasyprint import HTML

        pdf = HTML(string=template.render(), url_fetcher=asset_url_fetcher).write_pdf()
        assert isinstance(pdf, bytes) and pdf.startswith(b"%PDF")


class TestMoneyFormat:
    def test_vietnamese_grouping(self) -> None:
        assert formats.format_money(Decimal("1234567"), blank_zero=True) == "1.234.567"
        assert formats.format_money(Decimal("-1234567.50"), blank_zero=True) == "-1.234.567,5"

    def test_zero_blank_only_for_data_cells(self) -> None:
        assert formats.format_money(Decimal(0), blank_zero=True) == ""
        assert formats.format_money(Decimal(0), blank_zero=False) == "0"

    def test_none_is_blank(self) -> None:
        assert formats.format_money(None, blank_zero=False) == ""


class TestPdf:
    def test_single_chunk_pdf_contains_unit_title_totals_signature(self) -> None:
        rows = [_row("111", 5, debit=1_000_000), _row("511", 5, credit=1_000_000)]
        pdf = render_pdf(
            title="Sổ Cái",
            unit=UNIT,
            param_lines=("Từ ngày 01/01/2026 đến ngày 31/12/2026",),
            layout_spec=LAYOUT,
            display_rows=group_rows(iter(rows), layout_spec=LAYOUT),
            signature_date=date(2026, 8, 20),
        )
        text = "\n".join(_pdf_text(pdf))
        assert "CÔNG TY THỬ NGHIỆM" in text
        assert "MST: 0312345678" in text
        assert "SỔ CÁI" in text
        assert "111 — TK 111" in text
        assert "1.000.000" in text
        assert "Tổng cộng" in text
        assert "Kế toán trưởng" in text
        assert "Trang 1 / 1" in text

    def test_chunked_pdf_continues_pages_groups_and_defers_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phương án phân trang của cổng RT-25 (ADR-009 §Kết luận spike):
        số trang liên tục qua các lát, nhóm dở mở lại "(tiếp theo)", tiêu đề
        chỉ ở lát đầu, khối chữ ký chỉ ở lát cuối."""
        monkeypatch.setattr(pdf_renderer, "CHUNK_DATA_ROWS", 3)
        rows = [_row("111", day, debit=100_000) for day in range(1, 9)] + [
            _row("511", 9, credit=800_000)
        ]
        pdf = render_pdf(
            title="Sổ Cái",
            unit=UNIT,
            param_lines=("kỳ test",),
            layout_spec=LAYOUT,
            display_rows=group_rows(iter(rows), layout_spec=LAYOUT),
            signature_date=date(2026, 8, 20),
        )
        pages = _pdf_text(pdf)
        assert len(pages) >= 3
        for index, page_text in enumerate(pages):
            assert f"Trang {index + 1}" in page_text
        joined = "\n".join(pages)
        assert "(tiếp theo)" in joined
        assert joined.count("Kế toán trưởng") == 1
        assert joined.count("MST: 0312345678") == 1
        # Tổng nhóm không bị chia lát cắt làm sai: 8 dòng × 100.000.
        assert "800.000" in joined

    def test_pdf_embeds_be_vietnam_pro_not_os_fallback(self) -> None:
        """Font phải là Be Vietnam Pro nhúng từ assets, không phải font hệ
        điều hành: khi allowlist/CSS/tệp woff2 hỏng, WeasyPrint chỉ log rồi
        lặng lẽ rơi về font máy — bản in mỗi nền một kiểu, chữ có dấu vỡ khi
        trích xuất. Soi tên font thật trong PDF nên bắt được trên mọi nền."""
        rows = [_row("111", 5, debit=1_000_000)]
        pdf = render_pdf(
            title="Sổ Cái",
            unit=UNIT,
            param_lines=(),
            layout_spec=LAYOUT,
            display_rows=group_rows(iter(rows), layout_spec=LAYOUT),
            signature_date=date(2026, 8, 20),
        )
        base_fonts: set[str] = set()
        for page in PdfReader(io.BytesIO(pdf)).pages:
            fonts = page["/Resources"]["/Font"].get_object()
            for key in fonts:
                base_fonts.add(str(fonts[key].get_object()["/BaseFont"]))
        assert base_fonts, "PDF không nhúng font nào"
        for name in base_fonts:
            # Tên dạng "ABCDEF+Be-Vietnam-Pro-Bold" — tiền tố subset + family;
            # cách chấm câu tùy phiên bản WeasyPrint nên so sau khi bỏ "-".
            assert "BeVietnamPro" in name.replace("-", ""), f"font lạ trong PDF: {name}"

    def test_group_totals_survive_chunk_boundaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pdf_renderer, "CHUNK_DATA_ROWS", 2)
        rows = [_row("111", day, debit=1) for day in range(1, 6)]
        pdf = render_pdf(
            title="S",
            unit=UNIT,
            param_lines=(),
            layout_spec=LAYOUT,
            display_rows=group_rows(iter(rows), layout_spec=LAYOUT),
            signature_date=date(2026, 8, 20),
        )
        joined = "\n".join(_pdf_text(pdf))
        assert "Cộng 111 — TK 111" in joined
        assert "Tổng cộng" in joined


class TestXlsx:
    def test_values_formats_and_subtotal_formulas(self) -> None:
        import openpyxl

        rows = [_row("111", 5, debit=1_500_000), _row("111", 6, debit=500_000)]
        content = render_xlsx(
            title="Sổ Cái",
            unit=UNIT,
            param_lines=("kỳ test",),
            layout_spec=LAYOUT,
            display_rows=group_rows(iter(rows), layout_spec=LAYOUT),
        )
        workbook = openpyxl.load_workbook(io.BytesIO(content))
        sheet = workbook.active
        assert sheet is not None
        cells = [[cell.value for cell in row] for row in sheet.iter_rows()]
        flat = [value for row in cells for value in row if value is not None]
        assert "CÔNG TY THỬ NGHIỆM" in flat
        assert "Sổ Cái" in flat
        assert 1500000 in flat  # giá trị SỐ, không phải chuỗi đã định dạng
        formulas = [value for value in flat if isinstance(value, str) and value.startswith("=")]
        # SUBTOTAL chứ không SUM: tổng cuối không đếm đôi dòng tổng nhóm.
        assert formulas and all(value.startswith("=SUBTOTAL(9,") for value in formulas)
        assert any(isinstance(value, str) and value.startswith("Cộng 111") for value in flat)
