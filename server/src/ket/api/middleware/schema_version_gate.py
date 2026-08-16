"""Khóa phiên bản client ↔ server (bước 19, LD-05, FR-NFR-054).

Vấn đề mà cổng này giải: bản cài chạy trong LAN, mỗi máy trạm tự cập nhật theo
nhịp riêng, và một máy trạm bỏ quên vài tuần sẽ chạy binary cũ trên schema mới.
Bút toán do bản cũ ghi ra không lỗi — nó chỉ **thiếu** thứ mà schema mới yêu
cầu, và triệu chứng đầu tiên là một con số lệch ở kỳ quyết toán.

Hình dạng cổng (plan.md §Auto-update + khóa schema-version):

    client cũ hơn `min_client_version` → mọi lệnh GHI trả 426, đọc vẫn chạy
    client mới hơn server                → server không chặn; client tự cảnh báo
                                            từ kết quả `/system/handshake`

**Đọc không bị chặn** là phần cố ý: một văn phòng đang chờ nâng cấp vẫn phải tra
cứu được sổ sách, và chế độ chỉ-đọc chính là thứ khiến một lần tăng
`min_client_version` nhầm không làm cả văn phòng dừng việc.

**Thiếu header thì chặn** (quyết định H2). Một cổng cho qua khi vắng header chỉ
chặn được client trung thực: bản cũ chỉ cần bỏ header là đi vòng qua đúng cơ
chế sinh ra để chặn nó. Cùng hướng với `X-Dataset` — thiếu là lỗi, không có mặc
định nào được đoán hộ.

Miễn trừ **năm** đường của `/auth`, xem `EXEMPT_PATHS`: chúng ghi vào schema
điều khiển chứ không vào sổ sách, và thiếu chúng thì "chỉ-đọc" không tồn tại cho
tài khoản mang mật khẩu tạm hay chưa đăng ký 2FA.

Vì sao là **middleware** chứ không dependency (ngược với `dataset_context`, xem
`api/dependencies.py`): kiểm tra ở đây thuần header, không chạm DB, không cần
nằm trong transaction nghiệp vụ — và nó phải áp cho **mọi** route kể cả route
được thêm sau mà người viết quên khai dependency. Đổi lại, middleware chạy
ngoài phạm vi `add_exception_handler` nên nó **không** ném `DomainError` mà tự
dựng phản hồi bằng `problem_response` — cùng một hàm mà handler dùng, nên API
vẫn chỉ có một định dạng lỗi.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ket.api.middleware.problem_details import problem_response
from ket.kernel.errors import ClientVersionUnsupportedError
from ket.kernel.versions import Version, parse_version

logger = structlog.get_logger(__name__)

CLIENT_VERSION_HEADER: Final[str] = "X-Client-Version"
"""Phiên bản bản client đang gọi, dạng `MAJOR.MINOR.PATCH`.

Cùng con số ở `client/package.json` và `client/src-tauri/tauri.conf.json` —
`make version-check` canh cho năm tệp khai phiên bản không trôi khỏi nhau.
"""

WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""Chỉ lệnh ghi bị chặn. `GET`/`HEAD`/`OPTIONS` là chế độ chỉ-đọc."""

EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/change-password",
        "/api/v1/auth/totp/enroll",
        "/api/v1/auth/totp/confirm",
    }
)
"""Năm đường `POST` **không** bị cổng chặn. Danh sách đóng, và mỗi mục có một lý
do bắt buộc — không phải sự tiện tay.

Hai đường phiên:

* `login` — chế độ chỉ-đọc cần một phiên đăng nhập, mà phiên thì phải đăng nhập
  mới có. Chặn ở đây biến "chỉ-đọc" thành "không dùng được gì", tức là xóa mất
  chính cơ chế giữ cho cổng này an toàn.
* `logout` — thu hồi phiên phải chạy được ở **mọi** trạng thái. Một phiên không
  đăng xuất được là một phiên sống tới lúc hết hạn trên một máy mà người dùng
  tưởng đã rời khỏi.

Ba đường tài khoản (nới sau review lát 2C-1, đo được trên bản cài thật). Tài
khoản mang mật khẩu tạm, hoặc thuộc diện bắt buộc 2FA mà chưa đăng ký thiết bị,
**không đọc được gì cả**: mọi endpoint đọc đi qua `get_current_principal`, và
dependency đó đóng cả hai trạng thái. Chặn thêm ba đường dưới đây thì hai loại
tài khoản đó không phải "chỉ-đọc" mà là **khóa cứng** — và một bản cài mới, nơi
mọi tài khoản đều mang mật khẩu tạm, sẽ không ai vào được cho tới khi có người
chạm vào cấu hình máy chủ:

* `change-password`
* `totp/enroll`
* `totp/confirm`

Vì sao nới ba đường này **không** làm yếu cổng: chúng ghi vào **schema điều
khiển** (bảng tài khoản, phiên), không ghi vào sổ sách. Rủi ro mà cổng sinh ra
để chặn là "binary cũ ghi bút toán vào cấu trúc mới"; schema điều khiển đã có
cổng riêng canh — `verify_control_schema` từ chối khởi động khi nó lệch.
"""


class SchemaVersionGateMiddleware(BaseHTTPMiddleware):
    """Chặn lệnh ghi từ client cũ hơn `minimum_client_version`."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, minimum: Version) -> None:
        super().__init__(app)
        self._minimum = minimum

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rejection = self._rejection_for(request)
        if rejection is not None:
            return rejection
        return await call_next(request)

    def _rejection_for(self, request: Request) -> Response | None:
        """`None` = cho đi tiếp. Tách khỏi `dispatch` để test gọi thẳng được."""
        if request.method not in WRITE_METHODS:
            return None
        if request.url.path in EXEMPT_PATHS:
            return None

        raw = request.headers.get(CLIENT_VERSION_HEADER, "").strip()
        client_version = parse_version(raw) if raw else None
        if client_version is not None and client_version >= self._minimum:
            return None

        minimum = _format(self._minimum)
        detail = (
            f"Bản client này không ghi được vào máy chủ (cần {minimum} trở lên). "
            "Cập nhật ứng dụng rồi thao tác lại; trong lúc chờ vẫn tra cứu được."
            if raw
            else (
                f"Request không khai {CLIENT_VERSION_HEADER} nên không kiểm được "
                f"phiên bản client (cần {minimum} trở lên)."
            )
        )
        logger.info(
            "client_version_rejected",
            path=request.url.path,
            method=request.method,
            client_version=raw or None,
            minimum_client_version=minimum,
        )
        return problem_response(
            status=ClientVersionUnsupportedError.http_status,
            error_code=ClientVersionUnsupportedError.error_code,
            detail=detail,
            request=request,
            extra={
                "details": {
                    "client_version": raw or None,
                    "min_client_version": minimum,
                }
            },
        )


def _format(version: Version) -> str:
    major, minor, patch = version
    return f"{major}.{minor}.{patch}"
