"""Tùy chọn trình bày một lượt render (FR-RPT-010/012) — đọc từ settings ở
tầng API (`api/render_options.py`), truyền xuống dưới dạng giá trị bất biến.

Là dataclass truyền vào chứ không phải lượt đọc settings trong renderer:
renderer không có `user_id` (settings phân giải hai cấp) và không nên tự chạm
DB giữa pha giấy — cùng lý do `NumberingRule` là giá trị truyền vào.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_QUANTITY_DECIMALS = 2
DEFAULT_FONT_SIZE_PT = 9
"""Khớp `body { font-size: 9pt }` trong `print_base.css` — đổi một bên phải
đổi bên kia (test canh giá trị mặc định của catalog khớp hằng này)."""


@dataclass(frozen=True)
class LogoAsset:
    """Logo đơn vị (FR-RPT-010) — nội dung đã đọc sẵn từ kho content-addressed."""

    content: bytes
    media_type: str


@dataclass(frozen=True)
class RenderOptions:
    quantity_decimals: int = DEFAULT_QUANTITY_DECIMALS
    font_size_pt: int = DEFAULT_FONT_SIZE_PT
    logo: LogoAsset | None = None


DEFAULT_RENDER_OPTIONS = RenderOptions()
"""Singleton mặc định cho tham số hàm (ruff B008 cấm gọi hàm trong default)."""
