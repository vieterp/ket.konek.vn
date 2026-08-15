"""Hạn mức request theo người gọi (plan.md §Luồng request).

Bản cài chạy trong LAN của một doanh nghiệp, không phơi ra Internet — nên đây
**không** phải lá chắn chống DDoS. Nó chặn ba thứ có thật ở quy mô đó:

1. **Dò mật khẩu từ trong mạng.** Một máy trạm nhiễm mã độc thử vài nghìn mật
   khẩu là chuyện xảy ra được; khóa tài khoản sau N lần sai (lát 2B-1a) chặn
   theo *tài khoản*, còn ở đây chặn theo *người gọi* — kẻ dò 500 tài khoản khác
   nhau mỗi lần một lần thử sẽ không chạm ngưỡng khóa nào cả.
2. **Client hỏng lặp vô hạn.** Một vòng `useEffect` sai ở màn hình nào đó gửi
   request liên tục và làm chậm cả văn phòng; hạn mức biến sự cố đó thành một
   màn hình lỗi ở đúng một máy.
3. **Kịch bản chạy nhầm.** Cùng lý do trên, ở phía script tích hợp.

**Định danh ngân sách luôn neo vào địa chỉ IP** — không bao giờ chỉ dựa vào thứ
người gọi tự khai. Một hạn mức khóa theo header `Authorization` là hạn mức mà kẻ
gọi tự chọn ngân sách của mình: đổi header mỗi lần là có ngân sách mới. Xem
`_caller` và `_ip_key`.

Cửa sổ cố định (fixed window) chứ không phải token bucket: ngưỡng của hệ này
rộng (hàng trăm request/phút cho một người dùng thật), nên nhược điểm duy nhất
của cửa sổ cố định — cho phép gấp đôi hạn mức ngay chỗ giáp ranh hai cửa sổ —
không đổi được gì về mặt bảo vệ, trong khi mã ít hơn hẳn.

**Đếm trong tiến trình**, không dùng Redis: bản cài một máy không được thêm thứ
phải cài (LD-01). Hệ quả trung thực: chạy hai tiến trình API thì mỗi tiến trình
có bộ đếm riêng, tức là hạn mức thực tế nhân đôi. Chấp nhận được vì đây là lưới
an toàn, không phải cơ chế tính phí; chỗ sửa khi cần chính xác là chuyển bộ đếm
xuống PostgreSQL — không phải thêm một dịch vụ nữa vào bản cài.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ket.api.middleware.problem_details import problem_response
from ket.kernel.errors import RateLimitedError

logger = structlog.get_logger(__name__)

NS_PER_SECOND: Final[int] = 1_000_000_000

WINDOW_NS: Final[int] = 60 * NS_PER_SECOND
"""Cửa sổ một phút, đo bằng **nano giây nguyên**.

`time.monotonic_ns()` chứ không `time.monotonic()`: mã nghiệp vụ của repo này
cấm `float` (ADR-015) và bộ quét áp cho cả `src/ket`. Ở đây không có tiền nào
để làm tròn sai, nhưng một ngoại lệ trong hàng rào đó là thứ sẽ được viện dẫn
lần sau — và số nguyên nano giây thì không mất gì cả."""

AUTH_BUCKET: Final[str] = "auth"
DEFAULT_BUCKET: Final[str] = "default"

IP_BUDGET_FACTOR: Final[int] = 4
"""Trần theo IP = trần theo người gọi × hệ số này.

Một máy trạm có thể phục vụ vài người dùng thật (máy dùng chung ở kho, ở quầy
thu ngân), nên trần IP phải rộng hơn trần một người — nhưng vẫn **hữu hạn**, vì
đó là thứ duy nhất chặn được kẻ tự xưng nhiều danh tính từ một máy."""

AUTH_PATH_PREFIX: Final[str] = "/api/v1/auth/"
"""Nhóm đường dẫn có hạn mức riêng, chặt hơn nhiều: đây là nơi mật khẩu được
thử, và một người dùng thật đăng nhập vài lần một ngày chứ không vài lần một
phút."""

EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/health"})
"""`/health` không tính hạn mức: nó là thứ script cài đặt và trình giám sát gọi
theo vòng lặp, và chặn nó nghĩa là báo động giả đúng lúc đang cần biết máy chủ
còn sống hay không. Nó cũng không chạm DB nên không có gì để bảo vệ."""

MAX_TRACKED_CALLERS: Final[int] = 4096
"""Trần số người gọi được nhớ cùng lúc. Chạm trần thì dọn các cửa sổ đã hết hạn
— nếu không, một kẻ giả mạo IP nguồn sẽ làm bộ nhớ tiến trình phình mãi, tức là
biến lá chắn thành chỗ bị tấn công."""


@dataclass
class _Window:
    started_ns: int
    count: int


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Đếm request theo (người gọi, nhóm đường dẫn) trong cửa sổ một phút.

    **Không cần khóa đồng bộ**: middleware của Starlette chạy trên vòng lặp sự
    kiện (một luồng), và toàn bộ đoạn đọc–tăng–ghi dưới đây không có `await` nào
    ở giữa, nên không có điểm nào để một coroutine khác chen vào. Handler đồng
    bộ chạy trong threadpool thì không chạm cấu trúc này.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        default_per_minute: int,
        auth_per_minute: int,
    ) -> None:
        super().__init__(app)
        self._default_per_minute = default_per_minute
        self._auth_per_minute = auth_per_minute
        self._windows: dict[tuple[str, str], _Window] = {}

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        bucket = AUTH_BUCKET if request.url.path.startswith(AUTH_PATH_PREFIX) else DEFAULT_BUCKET
        limit = self._auth_per_minute if bucket == AUTH_BUCKET else self._default_per_minute
        if limit <= 0:
            # `0` = tắt hạn mức cho nhóm này. Có ích lúc diễn tập tải và lúc gỡ
            # rối; ghi log để một bản cài chạy mãi với hạn mức đã tắt không âm
            # thầm như vậy.
            logger.debug("rate_limit_disabled", bucket=bucket)
            return await call_next(request)

        now = time.monotonic_ns()

        # Hai bộ đếm cho mỗi request, không phải một. Bộ thứ nhất theo **người
        # gọi** (chia nhỏ theo token để hai người trên một máy không tranh nhau);
        # bộ thứ hai theo **IP**, là trần tổng mà không lời tự xưng nào vượt qua
        # được. Trần IP đặt gấp `IP_BUDGET_FACTOR` lần vì một máy trạm có thể
        # phục vụ vài người dùng thật.
        exceeded = self._consume(key=(self._caller(request, bucket), bucket), limit=limit, now=now)
        if exceeded is None:
            exceeded = self._consume(
                key=(self._ip_key(request), bucket),
                limit=limit * IP_BUDGET_FACTOR,
                now=now,
            )
        if exceeded is None:
            return await call_next(request)

        retry_after, effective_limit = exceeded
        logger.warning("rate_limited", bucket=bucket, limit=effective_limit, path=request.url.path)
        return problem_response(
            status=RateLimitedError.http_status,
            error_code=RateLimitedError.error_code,
            detail="Quá nhiều yêu cầu trong một phút — thử lại sau giây lát",
            request=request,
            extra={"details": {"limit": effective_limit, "retry_after_seconds": retry_after}},
            headers={"Retry-After": str(retry_after)},
        )

    def _consume(self, *, key: tuple[str, str], limit: int, now: int) -> tuple[int, int] | None:
        """Tính một request vào một ngân sách. Trả `(retry_after, trần)` nếu vượt.

        `None` = còn suất. Tách thành hàm riêng vì mỗi request đi qua **hai**
        ngân sách (người gọi và IP) với cùng một phép đếm.
        """
        window = self._windows.get(key)
        if window is None or now - window.started_ns >= WINDOW_NS:
            window = _Window(started_ns=now, count=0)
            self._windows[key] = window
            self._prune(now)

        window.count += 1
        if window.count <= limit:
            return None

        remaining_ns = WINDOW_NS - (now - window.started_ns)
        # Chia lên (ceil) bằng số nguyên: `Retry-After` là số giây, và làm tròn
        # xuống sẽ bảo client thử lại **trước** khi cửa sổ mở.
        return max(1, -(-remaining_ns // NS_PER_SECOND)), limit

    def _caller(self, request: Request, bucket: str) -> str:
        """Định danh ngân sách. **Luôn neo vào địa chỉ IP.**

        Đây là chỗ bản đầu tiên của tệp này sai, và sai theo cách vô hiệu hóa
        toàn bộ cơ chế: nó lấy băm của header `Authorization` **thô** làm định
        danh, tức là để **kẻ gọi tự chọn ngân sách của mình**. Gửi
        `Bearer rac-1`, `Bearer rac-2`, … là mỗi request một ngân sách mới, và
        trần không bao giờ chạm tới — đúng mục tiêu số một mà tệp này tuyên bố
        chặn. Đo được: 50 lần thử đăng nhập liên tiếp, không lần nào bị chặn.

        Luật bây giờ:

        * nhóm `auth`: **chỉ** IP. Đăng nhập chưa có danh tính, nên mọi thứ khác
          trong request đều do người gọi bịa ra được.
        * nhóm còn lại: `(IP, băm token)`. Token vẫn tham gia — cùng một máy
          nhiều người dùng thì tách ngân sách là đúng — nhưng nó chỉ **chia
          nhỏ** ngân sách của một IP, không tạo ra ngân sách mới, vì IP đã có
          hạn mức riêng ở nhánh dưới (`_ip_key`).

        Băm token thay vì dùng thẳng: bộ đếm sống trong bộ nhớ tiến trình và có
        thể lọt vào một bản dump khi gỡ rối, còn token thì mang trọn quyền của
        một người dùng.
        """
        host = self._host(request)
        if bucket == AUTH_BUCKET:
            return f"ip:{host}"
        header = request.headers.get("authorization", "")
        if not header:
            return f"ip:{host}"
        digest = hashlib.sha256(header.encode("utf-8")).hexdigest()[:32]
        return f"ip:{host}|t:{digest}"

    def _ip_key(self, request: Request) -> str:
        """Ngân sách **theo IP**, đếm song song với ngân sách theo người gọi.

        Không có nó thì token vẫn là một đường nhân ngân sách: một máy trạm gửi
        1.000 token rác vẫn có 1.000 ngân sách con, mỗi cái dưới trần. Trần theo
        IP đóng đường đó lại — dù kẻ gọi tự xưng là ai, tổng lượng request từ
        một máy vẫn bị chặn.
        """
        return f"ipsum:{self._host(request)}"

    @staticmethod
    def _host(request: Request) -> str:
        client = request.client
        return client.host if client is not None else "?"

    def _prune(self, now: int) -> None:
        """Dọn cửa sổ đã hết hạn khi bảng chạm trần.

        **Không** `clear()` toàn bảng khi dọn xong mà vẫn quá trần — bản đầu
        tiên làm vậy, và nó biến việc bơm định danh giả thành **công tắc tắt
        hạn mức**: đẩy bảng qua trần là bộ đếm của mọi người dùng thật về 0.
        Nay bảng đầy thì loại các cửa sổ **cũ nhất** (LRU theo thời điểm mở),
        giữ lại đúng những người gọi đang hoạt động — kẻ bơm định danh giả tự
        đẩy chính mình ra vì mỗi định danh của nó chỉ dùng một lần.
        """
        if len(self._windows) <= MAX_TRACKED_CALLERS:
            return

        expired = [key for key, w in self._windows.items() if now - w.started_ns >= WINDOW_NS]
        for key in expired:
            del self._windows[key]

        overflow = len(self._windows) - MAX_TRACKED_CALLERS
        if overflow <= 0:
            return
        logger.warning("rate_limit_table_full", tracked=len(self._windows), evicted=overflow)
        oldest = sorted(self._windows, key=lambda key: self._windows[key].started_ns)[:overflow]
        for key in oldest:
            del self._windows[key]
