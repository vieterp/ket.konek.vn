"""Môi trường render mẫu in — hai hàng rào của RT-01 (Critical).

1. **Jinja2 `SandboxedEnvironment`, autoescape bật.** Mẫu in là dữ liệu (gói
   cấu hình bên thứ ba sẽ mang mẫu riêng ở 5D) — payload SSTI kiểu
   `{{ ''.__class__.__mro__ }}` phải chết bằng `SecurityError`, kể cả với mẫu
   builtin: một môi trường cho mọi mẫu thì không có đường "quên bật sandbox".
2. **`url_fetcher` allowlist cho WeasyPrint.** CSS/HTML độc có thể kéo
   `file:///etc/passwd` qua `<img src>` hay `@import`; fetcher này chỉ phục vụ
   scheme `asset:` trỏ vào TỆP ĐÃ ĐĂNG KÝ trong `assets/` — mọi URL khác
   (file://, http://, data:…) bị từ chối bằng `ValueError`, WeasyPrint ghi lỗi
   và bỏ tài nguyên, nội dung tệp không bao giờ vào PDF.
"""

from __future__ import annotations

from importlib import resources
from typing import Final

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from weasyprint.urls import URLFetcherResponse

_ASSETS_ROOT: Final = resources.files("ket.reporting.rendering").joinpath("assets")
_TEMPLATES_ROOT: Final = resources.files("ket.reporting").joinpath("templates")

ASSET_SCHEME: Final = "asset:"

_ALLOWED_ASSETS: Final[frozenset[str]] = frozenset(
    {
        "print_base.css",
        "fonts/be-vietnam-pro-latin-400-normal.woff2",
        "fonts/be-vietnam-pro-latin-600-normal.woff2",
        "fonts/be-vietnam-pro-latin-700-normal.woff2",
        "fonts/be-vietnam-pro-vietnamese-400-normal.woff2",
        "fonts/be-vietnam-pro-vietnamese-600-normal.woff2",
        "fonts/be-vietnam-pro-vietnamese-700-normal.woff2",
    }
)
"""Danh sách ĐÓNG — khẳng định bằng giá trị, không bằng `Path.exists()`: một
tệp lạ rơi vào thư mục assets không tự động trở thành tài nguyên phục vụ được."""

_MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".css": "text/css",
    ".woff2": "font/woff2",
}


def create_print_environment() -> SandboxedEnvironment:
    """Môi trường Jinja2 duy nhất cho MỌI mẫu in (docstring đầu tệp).

    `StrictUndefined`: biến chưa truyền nổ ngay lúc render — một mẫu in lệch
    context phải là lỗi nhìn thấy, không phải ô trống lặng lẽ trên bản in.
    Không loader tệp: mẫu nạp bằng chuỗi (builtin đọc từ package, mẫu 5D đọc từ
    DB) — không có đường `{% include %}` mò ra hệ tệp.
    """
    return SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)


def builtin_template_source(name: str) -> str:
    """Nguồn của một mẫu builtin trong `reporting/templates/`."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Tên mẫu builtin không hợp lệ: {name!r}")
    return _TEMPLATES_ROOT.joinpath(name).read_text("utf-8")


def asset_url_fetcher(url: str) -> URLFetcherResponse:
    """`url_fetcher` cho WeasyPrint — chỉ phục vụ `asset:<tên-đã-đăng-ký>`."""
    if not url.startswith(ASSET_SCHEME):
        raise ValueError(f"Tài nguyên ngoài allowlist bị từ chối: {url!r}")
    name = url[len(ASSET_SCHEME) :].lstrip("/")
    if name not in _ALLOWED_ASSETS:
        raise ValueError(f"Tài nguyên `asset:` chưa đăng ký: {name!r}")
    payload = _ASSETS_ROOT.joinpath(name).read_bytes()
    suffix = name[name.rfind(".") :]
    return URLFetcherResponse(url, body=payload, headers={"content-type": _MIME_BY_SUFFIX[suffix]})


def print_base_css() -> str:
    return _ASSETS_ROOT.joinpath("print_base.css").read_text("utf-8")
