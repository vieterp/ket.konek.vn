"""Mảnh không-DB của lát 5D: định dạng số lượng (FR-RPT-012), token năm trong
đánh số, tài nguyên theo-lượt của fetcher (logo, FR-RPT-010), render mẫu in
chứng từ (RT-01 + watermark FR-RPT-011) và loader mẫu builtin.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from jinja2.exceptions import SecurityError
from pypdf import PdfReader

from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.config.printing.seed import load_builtin_print_templates
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule, _expand_year_tokens
from ket.reporting.printing.template_service import (
    VoucherPrintContext,
    VoucherPrintLine,
    render_voucher_pdf,
)
from ket.reporting.rendering import formats
from ket.reporting.rendering.environment import make_asset_fetcher
from ket.reporting.rendering.header import UnitInfo
from ket.reporting.rendering.options import LogoAsset, RenderOptions


class TestQuantityFormat:
    """FR-RPT-012: số lượng hiển thị đủ số lẻ cấu hình để cột thẳng hàng."""

    def test_fixed_decimals_pad_and_round_for_display_only(self) -> None:
        assert formats.format_quantity(Decimal(5), decimals=2) == "5,00"
        assert formats.format_quantity(Decimal("1234.5"), decimals=2) == "1.234,50"
        # HALF_UP hiển thị (review 5D, L2) — không phải banker's rounding.
        assert formats.format_quantity(Decimal("2.345"), decimals=2) == "2,35"
        assert formats.format_quantity(Decimal("-7.5"), decimals=1) == "-7,5"

    def test_zero_decimals_and_empty_cells(self) -> None:
        assert formats.format_quantity(Decimal(1500), decimals=0) == "1.500"
        assert formats.format_quantity(None, decimals=2) == ""
        assert formats.format_quantity(Decimal(0), decimals=2) == ""


class TestYearTokens:
    def test_tokens_expand_to_the_cycle_year(self) -> None:
        assert _expand_year_tokens("GLE{YY}-", on_date=date(2026, 5, 1)) == "GLE26-"
        assert _expand_year_tokens("{YYYY}/PT", on_date=date(2026, 5, 1)) == "2026/PT"

    def test_year_token_requires_yearly_reset(self) -> None:
        """Token năm trên dãy không-reset-năm là định dạng nói dối — chặn lúc
        khai quy tắc (trả nợ 4D, xem docstring `NumberingRule`)."""
        with pytest.raises(ValueError, match="YEARLY"):
            NumberingRule(document_type="X", prefix="X{YY}-", reset_rule=ResetRule.NEVER)
        NumberingRule(document_type="X", prefix="X{YY}-", reset_rule=ResetRule.YEARLY)


class TestPerRenderAssets:
    def test_extra_asset_is_served_and_everything_else_still_refused(self) -> None:
        fetcher = make_asset_fetcher({"logo": (b"png-bytes", "image/png")})
        served = fetcher("asset:logo")
        assert served.read() == b"png-bytes"
        assert served.content_type == "image/png"
        # Allowlist tĩnh vẫn phục vụ; mọi thứ khác vẫn chết như fetcher gốc.
        assert fetcher("asset:print_base.css").content_type == "text/css"
        with pytest.raises(ValueError, match="chưa đăng ký"):
            fetcher("asset:khong-co")
        with pytest.raises(ValueError, match="allowlist"):
            fetcher("file:///etc/passwd")

    def test_default_fetcher_does_not_know_per_render_names(self) -> None:
        fetcher = make_asset_fetcher()
        with pytest.raises(ValueError, match="chưa đăng ký"):
            fetcher("asset:logo")


def _context(*, draft: bool, copy_line: str | None = None) -> VoucherPrintContext:
    return VoucherPrintContext(
        title="Phiếu kế toán",
        voucher_no="GLE26-00042",
        document_date="01/03/2026",
        posting_date="01/03/2026",
        description="bút toán thử",
        draft=draft,
        copy_line=copy_line,
        lines=(
            VoucherPrintLine(
                line_no=1, account_code="642", description="chi phí", debit="123.000", credit=""
            ),
            VoucherPrintLine(
                line_no=2, account_code="111", description="chi phí", debit="", credit="123.000"
            ),
        ),
        total_debit="123.000",
        total_credit="123.000",
        signature_date_line="Ngày 01 tháng 03 năm 2026",
        unit=UnitInfo(name="Công ty thử", tax_code="0100000000", address="Hà Nội"),
    )


def _builtin_template() -> PrintTemplate:
    entry, html = next(
        item for item in load_builtin_print_templates() if item[0].code == "PHIEU-KE-TOAN"
    )
    return PrintTemplate(
        document_type=entry.document_type,
        code=entry.code,
        name=entry.name,
        html_template=html,
        css_extra=None,
        is_default=True,
        is_builtin=True,
    )


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


class TestVoucherTemplateRendering:
    def test_posted_voucher_renders_without_watermark(self) -> None:
        content = render_voucher_pdf(_builtin_template(), _context(draft=False))
        text = _pdf_text(content)
        assert "GLE26-00042" in text
        assert "123.000" in text
        assert "BẢN NHÁP" not in text

    def test_draft_carries_the_watermark_and_reprint_carries_the_copy_line(self) -> None:
        content = render_voucher_pdf(
            _builtin_template(), _context(draft=True, copy_line="In lần 3")
        )
        text = _pdf_text(content)
        assert "BẢN NHÁP" in text
        assert "In lần 3" in text

    def test_logo_flows_through_the_per_render_allowlist(self) -> None:
        # PNG 1×1 hợp lệ — fetcher chỉ phục vụ tên "logo" đã đăng ký cho lượt
        # render này; nội dung đi vào PDF mà không mở scheme nào khác.
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
            b"\xc0\xf0\x1f\x00\x05\x05\x02\x00_\xc8\xf1\xd2\x00\x00\x00\x00IEND"
            b"\xaeB`\x82"
        )
        content = render_voucher_pdf(
            _builtin_template(),
            _context(draft=False),
            options=RenderOptions(logo=LogoAsset(content=png, media_type="image/png")),
        )
        assert content.startswith(b"%PDF")

    def test_ssti_in_a_template_dies_in_the_sandbox(self) -> None:
        """RT-01: mẫu (kể cả người dùng sửa) chạy trong SandboxedEnvironment."""
        hostile = PrintTemplate(
            document_type="GLE",
            code="DOC-HAI",
            name="mẫu độc",
            html_template="{{ ''.__class__.__mro__ }}",
            css_extra=None,
            is_default=False,
            is_builtin=False,
        )
        with pytest.raises(SecurityError):
            render_voucher_pdf(hostile, _context(draft=False))

    def test_css_extra_cannot_reach_the_filesystem(self) -> None:
        """`css_extra` là CSS không tin cậy — `url()` đi qua fetcher allowlist
        nên `file://` không đưa được nội dung tệp vào PDF (render vẫn xong,
        WeasyPrint ghi lỗi và bỏ tài nguyên)."""
        template = _builtin_template()
        hostile = PrintTemplate(
            document_type=template.document_type,
            code="CSS-DOC",
            name=template.name,
            html_template=template.html_template,
            css_extra='body { background-image: url("file:///etc/passwd") }',
            is_default=False,
            is_builtin=False,
        )
        content = render_voucher_pdf(hostile, _context(draft=False))
        assert content.startswith(b"%PDF")
        assert b"root:" not in content


class TestBuiltinTemplateManifest:
    def test_builtin_manifest_loads_and_has_the_gle_default(self) -> None:
        loaded = load_builtin_print_templates()
        by_key = {(entry.document_type, entry.code): entry for entry, _html in loaded}
        assert by_key[("GLE", "PHIEU-KE-TOAN")].is_default is True
