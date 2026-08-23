"""Kết xuất PDF: dòng trình bày → HTML (Jinja2 sandbox) → WeasyPrint (RT-01).

Tách hai pha có chủ đích:

* **Pha chữ** (`presentation.py` + `kernel/formatting.py`, dùng chung preview JSON
  của lát 5D): mọi giá trị thành chuỗi TRƯỚC khi vào template — template chỉ
  đổ khuôn, autoescape lo phần còn lại.
* **Pha giấy** (WeasyPrint): nhận HTML + `print_base.css` (tokens Konek) +
  font nhúng qua `asset_url_fetcher` — PDF giống nhau trên Windows/macOS
  (font đi trong server, không mượn của hệ điều hành).

`weasyprint` không có stub kiểu — import gói gọn trong tệp này, phần còn lại
của `reporting` không thấy `Any` của nó (mypy override khai ở `pyproject.toml`
kèm lý do).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import date

from pypdf import PdfReader, PdfWriter
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

from ket.kernel.config.reports.spec import LayoutSpec
from ket.reporting.engine.grouping import (
    DataRow,
    DisplayRow,
    GroupFooter,
    GroupHeader,
)
from ket.reporting.rendering.environment import (
    builtin_template_source,
    create_print_environment,
    make_asset_fetcher,
    print_base_css,
)
from ket.reporting.rendering.header import UnitInfo, signature_date_line
from ket.reporting.rendering.options import (
    DEFAULT_FONT_SIZE_PT,
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
)
from ket.reporting.rendering.presentation import presentation_rows

LOGO_ASSET_NAME = "logo"
"""Tên tài nguyên theo-lượt của logo trong fetcher (`asset:logo`)."""

_TEMPLATE_NAME = "report_table.html.j2"


CHUNK_DATA_ROWS = 1500
"""Số dòng dữ liệu mỗi lát render (spike S2): WeasyPrint tốn ~3,4ms/dòng và
~200MB RAM/1.000 dòng — render nguyên khối 50.000 dòng đo được 309s/7,2GB,
trượt cả hai ngưỡng RT-25. Chia lát giữ RAM đỉnh ~400MB bất kể độ dài sổ
(thời gian vẫn tuyến tính — ngưỡng thời gian sổ dài rebaseline ở ADR-009)."""


def render_pdf(
    *,
    title: str,
    unit: UnitInfo,
    param_lines: tuple[str, ...],
    layout_spec: LayoutSpec,
    display_rows: Iterator[DisplayRow],
    signature_date: date,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
) -> bytes:
    """Một báo cáo dạng bảng/nhóm → PDF hoàn chỉnh (tiêu đề, tham số, chữ ký).

    Báo cáo gọn (≤ `CHUNK_DATA_ROWS` dòng) render nguyên khối, chân trang
    "Trang X / Y". Sổ dài render **theo lát** rồi ghép bằng pypdf (phương án
    phân trang của cổng RT-25): mỗi lát tiếp số trang bằng
    `@page:first {{ counter-reset: page N }}` (đã kiểm chứng trên WeasyPrint 69),
    lát sau mở lại nhóm đang dở với "(tiếp theo)"; tiêu đề chỉ ở lát đầu, khối
    chữ ký chỉ ở lát cuối. Chân trang chế độ lát là "Trang X" — tổng số trang
    chỉ biết sau lát cuối, và một con số "/ Y" sai còn tệ hơn không có.
    """
    environment = create_print_environment()
    template = environment.from_string(builtin_template_source(_TEMPLATE_NAME))
    font_config = FontConfiguration()
    fetcher = make_asset_fetcher(
        {LOGO_ASSET_NAME: (options.logo.content, options.logo.media_type)}
        if options.logo is not None
        else None
    )
    base_css = CSS(string=print_base_css(), url_fetcher=fetcher, font_config=font_config)
    override_css: CSS | None = None
    if options.font_size_pt != DEFAULT_FONT_SIZE_PT:
        # FR-RPT-010: cỡ chữ thân bản in theo cấu hình — một override nhỏ đè
        # lên `print_base.css`, họ font giữ nguyên token thương hiệu.
        override_css = CSS(
            string=f"body {{ font-size: {int(options.font_size_pt)}pt }}",
            font_config=font_config,
        )

    chunks = _chunk_display_rows(display_rows, layout_spec=layout_spec)
    first_chunk = next(chunks)
    try:
        second_chunk: list[DisplayRow] | None = next(chunks)
    except StopIteration:
        second_chunk = None

    def render_chunk(
        rows: list[DisplayRow], *, is_first: bool, is_last: bool, page_offset: int
    ) -> bytes:
        html_text = template.render(
            title=title,
            unit=unit,
            param_lines=param_lines,
            orientation=layout_spec.page.orientation,
            columns=layout_spec.columns,
            display_rows=presentation_rows(
                iter(rows),
                layout_spec=layout_spec,
                quantity_decimals=options.quantity_decimals,
            ),
            signature_date_line=signature_date_line(signature_date),
            show_title=is_first,
            show_signature=is_last,
            continued=not is_first,
            logo_url=f"asset:{LOGO_ASSET_NAME}" if options.logo is not None else None,
        )
        stylesheets = [base_css]
        if override_css is not None:
            stylesheets.append(override_css)
        if not (is_first and is_last):
            stylesheets.append(
                CSS(
                    string=(
                        '@page { @bottom-right { content: "Trang " counter(page) } }\n'
                        f"@page:first {{ counter-reset: page {page_offset + 1} }}"
                    ),
                    font_config=font_config,
                )
            )
        document = HTML(string=html_text, url_fetcher=fetcher, base_url="asset:")
        output = document.write_pdf(stylesheets=stylesheets, font_config=font_config)
        if not isinstance(output, bytes):  # pragma: no cover - hợp đồng weasyprint
            raise TypeError("weasyprint.write_pdf không trả bytes")
        return output

    if second_chunk is None:
        return render_chunk(first_chunk, is_first=True, is_last=True, page_offset=0)

    writer = PdfWriter()
    pages_done = 0
    pending: list[DisplayRow] | None = first_chunk
    upcoming: list[DisplayRow] | None = second_chunk
    is_first = True
    while pending is not None:
        is_last = upcoming is None
        part = render_chunk(pending, is_first=is_first, is_last=is_last, page_offset=pages_done)
        reader = PdfReader(io.BytesIO(part))
        for page in reader.pages:
            writer.add_page(page)
        pages_done += len(reader.pages)
        is_first = False
        pending = upcoming
        upcoming = next(chunks, None)
    merged = io.BytesIO()
    writer.write(merged)
    return merged.getvalue()


def _chunk_display_rows(
    display_rows: Iterator[DisplayRow], *, layout_spec: LayoutSpec
) -> Iterator[list[DisplayRow]]:
    """Cắt dòng trình bày thành lát ~`CHUNK_DATA_ROWS` dòng dữ liệu.

    Lát sau mở đầu bằng tiêu đề các nhóm đang dở kèm "(tiếp theo)" — người đọc
    trang giữa sổ vẫn biết mình đang ở tài khoản nào. Luôn cho ra ít nhất một
    lát (báo cáo rỗng = một lát chỉ có dòng tổng).
    """
    open_headings: list[str] = []
    buffer: list[DisplayRow] = []
    data_rows = 0
    for row in display_rows:
        if isinstance(row, GroupHeader):
            del open_headings[row.level :]
            open_headings.append(row.heading)
        elif isinstance(row, GroupFooter):
            del open_headings[row.level :]
        buffer.append(row)
        if isinstance(row, DataRow):
            data_rows += 1
            if data_rows >= CHUNK_DATA_ROWS:
                yield buffer
                buffer = [
                    GroupHeader(level=level, heading=f"{heading} (tiếp theo)")
                    for level, heading in enumerate(open_headings)
                ]
                data_rows = 0
    yield buffer
