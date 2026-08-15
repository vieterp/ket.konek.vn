"""Cổng vào của idempotency ở tầng HTTP (FR-NFR-004, RT-12).

Hai việc, và việc thứ hai mới là việc khó:

1. Đọc `X-Idempotency-Key`, kiểm rồi trả cho endpoint.
2. **Đánh dấu** endpoint là đã khai idempotency, để một test duyệt được toàn bộ
   bảng route và bắt endpoint `POST` nào quên khai.

Vì sao cần việc thứ hai: quy ước "mọi POST đổi trạng thái phải có
`X-Idempotency-Key`" (plan.md §Quy ước REST API) là loại quy ước sẽ mục dần —
phase 6 trở đi thêm hàng chục endpoint ghi chứng từ, và người viết endpoint thứ
bốn mươi sẽ chép từ một endpoint khác chứ không đọc lại plan. Danh sách **miễn
trừ** vì thế nằm ở đây, trong mã nguồn có review, chứ không nằm trong tệp test:
thêm một ngoại lệ phải là một thay đổi mà người review nhìn thấy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Final

from fastapi import Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import String
from starlette.routing import BaseRoute

from ket.kernel.errors import IdempotencyKeyInvalidError, IdempotencyKeyMissingError
from ket.kernel.idempotency.models import IdempotencyKey

IDEMPOTENCY_HEADER: Final[str] = "X-Idempotency-Key"

MAX_KEY_LENGTH: Final[int] = 120
"""Bằng độ rộng cột `idempotency_keys.idempotency_key`. Đủ cho UUID, ULID hay
bất kỳ dạng khóa nào client sinh; dài hơn thế là dấu hiệu client đang nhét dữ
liệu vào khóa."""

_ROUTE_ATTRIBUTE: Final[str] = "ket_idempotent_route"

IDEMPOTENCY_EXEMPT_PREFIXES: Final[tuple[str, ...]] = (
    # Xác thực: `login`/`logout`/`change-password` đổi trạng thái **phiên**, chứ
    # không ghi dữ liệu kế toán. Gửi lại một lần đăng nhập chỉ tạo thêm một
    # phiên — vô hại và thu hồi được — trong khi bắt buộc khóa ở đây có nghĩa là
    # màn hình đăng nhập phải sinh khóa trước khi biết mình là ai.
    "/api/v1/auth/",
    # Kết xuất báo cáo (phase 5) và hàng đợi job (lát 2B-2b): FR-NFR-004 miễn
    # trừ tường minh. Cả hai đều **không** đổi trạng thái nghiệp vụ — một cái
    # đọc, một cái chỉ xếp hàng; và cả hai đều là POST vì tham số quá lớn cho
    # query string.
    "/api/v1/reports/",
    # Dấu `/` cuối là bắt buộc: `"/api/v1/jobs"` trần cũng miễn trừ luôn
    # `/api/v1/jobsomething` — một endpoint ghi ở phase sau chỉ cần trùng tiền
    # tố là lọt qua cổng mà không ai thấy. Đường `/api/v1/jobs` đúng nghĩa nằm
    # trong `IDEMPOTENCY_EXEMPT_PATHS`.
    "/api/v1/jobs/",
)

IDEMPOTENCY_EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {
        # Gán vai trò / gán chi nhánh: ghi **tư cách thành viên của một tập
        # hợp**, không tạo bản ghi mới. Gửi lại lần thứ hai cho ra đúng trạng
        # thái đó và trả `changed=false` — bản thân thao tác đã idempotent, nên
        # một khóa chỉ thêm nghi thức mà không ngăn được gì.
        #
        # Khác hẳn `POST /branches`: ở đó lần gửi thứ hai sinh ra chi nhánh thứ
        # hai. Ranh giới là "lặp lại có tạo thêm bản ghi không", không phải
        # "phương thức HTTP là gì".
        "/api/v1/system/users/{user_id}/roles",
        "/api/v1/system/users/{user_id}/branches",
        # Xếp một job vào hàng đợi (lát 2B-2b) — FR-NFR-004 miễn trừ tường minh.
        "/api/v1/jobs",
    }
)
"""Miễn trừ theo **đúng một đường dẫn**, cho thao tác tự nó đã idempotent.

Tách khỏi danh sách tiền tố vì rủi ro khác nhau: một tiền tố miễn trừ luôn cả
những endpoint chưa ai viết, còn danh sách này thì mỗi lần thêm là một quyết
định về một endpoint cụ thể."""


def idempotency_key_dependency(route_key: str) -> Callable[[Request], str]:
    """Dựng dependency đọc khóa cho **một** route đã khai.

    `route_key` là đường dẫn **khai báo** (`POST /api/v1/system/branches`), chưa
    điền tham số: khóa idempotency có phạm vi theo route, và nếu dùng URL đã
    điền `id` thì mỗi bản ghi lại thành một phạm vi riêng — tức là mất tác dụng
    đúng ở chỗ nó cần nhất, lúc tạo mới.
    """

    def dependency(request: Request) -> str:
        raw = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not raw:
            raise IdempotencyKeyMissingError(
                f"Thao tác này bắt buộc header {IDEMPOTENCY_HEADER}", route=route_key
            )
        if len(raw) > MAX_KEY_LENGTH:
            raise IdempotencyKeyInvalidError(
                f"Khóa idempotency dài quá {MAX_KEY_LENGTH} ký tự", route=route_key
            )
        if not raw.isascii() or not raw.isprintable():
            raise IdempotencyKeyInvalidError(
                "Khóa idempotency chỉ được chứa ký tự ASCII hiển thị được", route=route_key
            )
        return raw

    setattr(dependency, _ROUTE_ATTRIBUTE, route_key)
    return dependency


def iter_api_routes(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    """Mọi `APIRoute` của ứng dụng, kể cả route nằm trong router đã `include`.

    FastAPI từ 0.14x **không** trải phẳng router con vào `app.routes` nữa: nó
    chèn một đối tượng bọc và giữ router gốc bên trong. Một vòng lặp
    `for route in app.routes` vì thế chỉ thấy vài route gốc — và một test
    coverage dựa vào nó sẽ **xanh vì không thấy gì**, đúng kiểu hỏng tệ nhất cho
    một cổng kiểm tra.

    Duyệt theo cả hai kiểu bọc (thuộc tính `routes` hoặc `original_router`) để
    không phải sửa lại khi FastAPI đổi cách biểu diễn lần nữa.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested is not None:
            yield from iter_api_routes(nested)


def declared_route_key(route: BaseRoute) -> str | None:
    """Route này khai idempotency cho khóa nào, nếu có.

    Duyệt **cả cây** dependency chứ không chỉ tầng đầu: một endpoint có thể nhận
    khóa qua một dependency gộp (như `Authorized` gộp dataset + quyền), và bỏ
    sót nhánh con sẽ khiến test coverage báo nhầm là "quên khai".
    """
    if not isinstance(route, APIRoute):
        return None
    return _search(route.dependant)


def _search(dependant: Dependant) -> str | None:
    key = getattr(dependant.call, _ROUTE_ATTRIBUTE, None)
    if isinstance(key, str):
        return key
    for child in dependant.dependencies:
        found = _search(child)
        if found is not None:
            return found
    return None


def is_exempt(path: str) -> bool:
    """Đường dẫn nằm trong danh sách miễn trừ của FR-NFR-004?"""
    return path in IDEMPOTENCY_EXEMPT_PATHS or path.startswith(IDEMPOTENCY_EXEMPT_PREFIXES)


def route_key_fits_column(route_key: str) -> bool:
    """Khóa route có vừa cột `idempotency_keys.route_key` không.

    Kiểm được **lúc test** (xem test coverage) thay vì lúc chạy: một route quá
    dài sẽ chỉ lộ ra khi có người dùng thật gửi request đầu tiên tới nó.
    """
    column_type = IdempotencyKey.__table__.c.route_key.type
    if not isinstance(column_type, String) or column_type.length is None:
        return True
    return len(route_key) <= column_type.length
