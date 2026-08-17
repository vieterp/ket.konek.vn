"""Chặn thân request quá lớn **trước khi** nó chạm đĩa (FR-NFR-053, C1).

Vì sao phải là middleware ASGI chứ không phải một phép kiểm trong endpoint: với
endpoint nhận `multipart/form-data`, FastAPI gọi `await request.form()` **trước**
`solve_dependencies` (`fastapi/routing.py::get_request_handler`), và
`starlette.formparsers.MultiPartParser` đổ phần **tệp** vào một
`SpooledTemporaryFile` **không có trần**. Nghĩa là tới lúc dependency xác thực
chạy — chứ đừng nói tới lúc mã đính kèm nhìn thấy byte đầu tiên — toàn bộ tệp đã
nằm trên đĩa máy chủ.

Hệ quả nếu không có tệp này, và nó đã được đo: một request **không mang token**
đẩy được 12 MiB xuống thư mục tạm của một bản cài đặt trần 1 KiB. Trên bản cài
một máy (LD-01) thư mục tạm dùng chung ổ với PostgreSQL, nên đây là đường làm
đầy ổ của cả cơ sở dữ liệu mà không cần đăng nhập.

Middleware ASGI **thuần** (không `BaseHTTPMiddleware`) vì đúng một lý do: nó cần
bọc `receive`, tức là đếm byte **trong lúc** chúng tới. `BaseHTTPMiddleware`
không cho làm điều đó — nó nhận một `Request` mà thân đã ở phía sau.

Hai trần, không phải một:

* `default_max_bytes` cho mọi endpoint — thân JSON của hệ này là chứng từ và
  danh mục, không có cái nào cần tới một megabyte;
* trần rộng hơn cho đúng nhóm `/api/v1/attachments`, nơi tệp là mục đích.

Một trần chung nới rộng cho vừa tệp đính kèm sẽ cho **mọi** endpoint quyền nhận
25 MiB — tức là đổi một lỗ lấy một lỗ rộng hơn.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Final

import structlog
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ket.api.middleware.problem_details import problem_response
from ket.kernel.errors import RequestBodyTooLargeError

logger = structlog.get_logger(__name__)

DEFAULT_MAX_BODY_BYTES: Final[int] = 1024 * 1024
"""Trần mặc định 1 MiB cho thân JSON.

Rộng gấp nhiều lần chứng từ nhiều dòng nhất mà lưới nhập liệu tạo ra (spike S3
đo trên 500 dòng), nên không người dùng thật nào chạm tới. Nó tồn tại để chặn
client hỏng và request cố ý, không để bó nghiệp vụ."""

MULTIPART_OVERHEAD_BYTES: Final[int] = 8 * 1024
"""Phần bao của `multipart/form-data` cộng thêm vào trần tệp.

Ranh giới, tên trường, tên tệp và header của từng phần đều nằm trong thân
request nhưng **không** phải nội dung tệp. Không cộng phần này thì một tệp đúng
bằng trần sẽ bị từ chối, và triệu chứng ("tệp 25 MB không tải lên được dù giới
hạn ghi là 25 MB") là loại lỗi tốn cả buổi để hiểu."""


class BodySizeLimitMiddleware:
    """Từ chối `413` khi thân request vượt trần, không đọc hết phần còn lại."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        overrides: tuple[tuple[str, int], ...] = (),
    ) -> None:
        self.app = app
        self.default_max_bytes = default_max_bytes
        # Sắp theo độ dài giảm dần: tiền tố cụ thể hơn thắng, không phụ thuộc
        # thứ tự người gọi khai.
        self.overrides = tuple(sorted(overrides, key=lambda item: len(item[0]), reverse=True))

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.overrides:
            if path.startswith(prefix):
                return limit
        return self.default_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope.get("path", ""))
        request = Request(scope)

        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            # Đường rẻ nhất: client trung thực khai `Content-Length`. Từ chối
            # ngay, chưa đọc byte nào của thân.
            await _reject(request, send, limit=limit, declared=declared)
            return

        state = _State()

        async def guarded_receive() -> Message:
            message = await receive()
            if message["type"] != "http.request":
                return message
            state.received += len(message.get("body", b""))
            if state.received <= limit:
                return message
            if not state.rejected:
                state.rejected = True
                await _reject(request, send, limit=limit, declared=None)
            # Báo cho ứng dụng phía dưới là kênh đã đứt. Nó sẽ dừng phân tích
            # multipart và bung ra ngoài; phản hồi `413` thì đã gửi rồi.
            return {"type": "http.disconnect"}

        async def guarded_send(message: Message) -> None:
            if state.rejected:
                # Đã trả `413`. Bỏ mọi thứ ứng dụng phía dưới cố gửi thêm — gửi
                # hai phản hồi cho một request là lỗi giao thức.
                return
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except ClientDisconnect:
            # Kênh đứt **do chính middleware này** cắt: đã trả lời rồi, không có
            # gì phải làm nữa. Kênh đứt vì lý do khác thì để nguyên cho lớp trên.
            if not state.rejected:
                raise


class _State:
    """Trạng thái của một request. Lớp chứ không `dict`: có kiểu (LD-13)."""

    __slots__ = ("received", "rejected")

    def __init__(self) -> None:
        self.received = 0
        self.rejected = False


def _declared_length(scope: MutableMapping[str, Any]) -> int | None:
    """`Content-Length` client khai, hoặc `None` nếu thiếu/không đọc được.

    Giá trị này **không** được tin làm cơ sở cho phép đo — nó chỉ là đường tắt
    để từ chối sớm. Phép đo thật là số byte đếm được trong `guarded_receive`.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(request: Request, send: Send, *, limit: int, declared: int | None) -> None:
    """Gửi `413` theo đúng khuôn RFC 7807 mà cả API dùng."""
    logger.warning(
        "request_body_too_large",
        path=request.url.path,
        limit_bytes=limit,
        declared_bytes=declared,
    )
    error = RequestBodyTooLargeError(
        "Thân request vượt quá dung lượng cho phép của bản cài", max_bytes=limit
    )
    response = problem_response(
        status=error.http_status,
        error_code=error.error_code,
        detail=error.message,
        request=request,
        extra={"details": error.details},
    )
    await response(request.scope, _empty_receive, send)


async def _empty_receive() -> Message:  # pragma: no cover - Response không gọi tới
    """`receive` giả cho phản hồi lỗi: nó không đọc thân request nào."""
    return {"type": "http.disconnect"}
