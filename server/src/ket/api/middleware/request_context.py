"""Mã tương quan cho mỗi request (FR-NFR-050, `audit_log.correlation_id`).

Một người dùng bấm một nút → nhiều dòng nhật ký, nhiều dòng log, có khi vài job
nền. `correlation_id` là thứ duy nhất nối chúng lại thành một câu chuyện đọc
được khi có tranh chấp số liệu.

Nhận từ client nếu client gửi (`X-Correlation-Id`), sinh mới nếu không — và
**luôn** trả lại trong header phản hồi, để người dùng đọc được mã đó trên màn
hình lỗi rồi đọc cho bộ phận hỗ trợ.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final
from uuid import UUID, uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER: Final[str] = "X-Correlation-Id"
CORRELATION_STATE_KEY: Final[str] = "correlation_id"

MAX_CLIENT_INFO_LENGTH: Final[int] = 255
"""Bằng độ rộng cột `audit_log.client_info` / `control_audit_log.client_info`.
Cắt ở đây thay vì để `INSERT` đổ giữa một thao tác ghi sổ vì một `User-Agent`
dài bất thường."""


def correlation_id_of(request: Request) -> UUID | None:
    """Mã tương quan của request đang xử lý, nếu middleware đã chạy."""
    value = getattr(request.state, CORRELATION_STATE_KEY, None)
    return value if isinstance(value, UUID) else None


def client_info_of(request: Request) -> str:
    """Mô tả ngắn về máy gọi, để ghi vào nhật ký: `<ip> <user-agent>`."""
    host = request.client.host if request.client is not None else "?"
    agent = request.headers.get("user-agent", "?")
    return f"{host} {agent}"[:MAX_CLIENT_INFO_LENGTH]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Gắn `correlation_id` vào `request.state`, log và header phản hồi."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = _incoming(request) or uuid4()
        setattr(request.state, CORRELATION_STATE_KEY, correlation_id)

        structlog.contextvars.bind_contextvars(
            correlation_id=str(correlation_id),
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_HEADER] = str(correlation_id)
        return response


def _incoming(request: Request) -> UUID | None:
    """Mã do client gửi, nếu nó là UUID hợp lệ.

    Giá trị rác bị bỏ qua chứ không làm hỏng request: đây là thông tin để lần
    vết, không phải dữ liệu nghiệp vụ, và từ chối cả request vì một header sai
    định dạng là đổi một phiền toái nhỏ lấy một lỗi lớn.
    """
    raw = request.headers.get(CORRELATION_HEADER)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
