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
        # In chứng từ (lát 5D, FR-RPT-011): thứ duy nhất nó ghi là một dòng
        # `print_log` — và in lần 2 là một SỰ KIỆN THẬT mà FR-RPT-011 sinh ra
        # để ĐẾM (`copy_no` + cảnh báo), không phải một lần gửi lại cần khử.
        # Một khóa idempotency ở đây sẽ trả bản PDF cũ và nói dối số lần in.
        "/api/v1/vouchers/{voucher_id}/print",
        # In biên bản kiểm kê quỹ (lát 6E-2): KHÔNG ghi gì cả — không cả một
        # dòng `print_log` (biên bản không phải chứng từ, sổ đếm lần in gắn
        # khóa ngoại tới `vouchers`). Là POST chỉ vì đứng cùng họ với nút In
        # chứng từ và trả về tệp nhị phân.
        "/api/v1/cash-book/count-sheets/{sheet_id}/print",
        # Nút một-bước gán logo (lát 5E, FR-RPT-010): gửi lại cùng tệp ghi lại
        # cùng content_hash và cùng hai giá trị settings — bản thân thao tác
        # idempotent theo nội dung, không có bản ghi nào bị nhân đôi (kho blob
        # content-addressed khử trùng theo hash).
        "/api/v1/system/settings/logo",
        # Nhập sao kê (lát 6D): lượt gửi lại đâm vào khóa băm-nội-dung per-TK
        # (`bank_statement.duplicate`, 409) — không thể nhân đôi sao kê, và
        # khóa ấy còn bền hơn khóa idempotency vì không hết hạn.
        "/api/v1/bank/statements/import",
        # Khai hồ sơ định dạng sao kê (lát 6G-2): lượt gửi lại đâm vào unique
        # `(bank_id, name)` của bảng (`bank_statement_profile.conflict`, 409) —
        # cùng lối miễn trừ với nhập sao kê ngay trên, và cùng lý do: ràng buộc
        # DB khử trùng bền hơn một khóa có hạn.
        "/api/v1/bank/statements/profiles",
        # Ba thao tác khớp/gỡ khớp (lát 6D) ghi **trạng thái của một tập dòng**,
        # cùng họ với gán vai trò ở trên: chạy lại cho ra đúng trạng thái đó
        # (auto-match lần 2 thấy 0 dòng chưa khớp; match lần 2 → 409 đã khớp;
        # unmatch lần 2 → 409 chưa khớp) — không lần gửi lại nào tạo thêm bản ghi.
        "/api/v1/bank/statements/{statement_id}/actions/auto-match",
        "/api/v1/bank/statements/lines/{line_id}/actions/match",
        "/api/v1/bank/statements/lines/{line_id}/actions/unmatch",
        # Định giá một dòng chứng từ sắp lập (lát 7C-1): **không ghi gì cả**. Là
        # POST chỉ vì đầu vào chín tham số — một `GET` với chín tham số truy vấn
        # là chín chỗ để client dựng URL sai mà không nhận được lỗi theo trường.
        # Cùng lối miễn trừ với biên bản kiểm kê quỹ ngay trên.
        "/api/v1/pricing/quote",
        # Cùng đường ấy, hỏi cả chứng từ một lượt (lát 7C-2) — vẫn không tạo
        # gì, nên vẫn không có gì để một khóa idempotency bảo vệ.
        "/api/v1/pricing/quote-batch",
    }
)
"""Miễn trừ theo **đúng một đường dẫn**, cho thao tác tự nó đã idempotent.

Tách khỏi danh sách tiền tố vì rủi ro khác nhau: một tiền tố miễn trừ luôn cả
những endpoint chưa ai viết, còn danh sách này thì mỗi lần thêm là một quyết
định về một endpoint cụ thể."""

IDEMPOTENCY_EXEMPT_SUFFIXES: Final[tuple[str, ...]] = (
    # Nhập liệu danh mục từ Excel (lát 3C-1). Hai endpoint này **xếp hàng**, y
    # như `/api/v1/jobs`: chúng tạo một *yêu cầu*, không ghi dữ liệu kế toán, và
    # cả hai loại job đều khai `IDEMPOTENT_RESTART`.
    #
    # Vì sao gửi lại vô hại, xét riêng từng cái:
    #
    # * `validate` chỉ đọc. Tệp đi vào kho định địa chỉ theo nội dung nên lượt
    #   thứ hai không tốn thêm byte nào, và nó sinh ra một báo cáo thứ hai giống
    #   hệt.
    # * `commit` ghi, nhưng ghi **lũy đẳng theo thiết kế** (H85): ở chế độ
    #   `create_only` lượt thứ hai thấy mọi mã đã tồn tại nên dừng với toàn dòng
    #   lỗi và không ghi gì; ở `create_and_update` nó ghi lại đúng cùng giá trị
    #   từ đúng cùng tệp — `content_hash` của lượt kiểm quyết định tệp nào được
    #   đọc, và client không khai lại được giá trị đó.
    #
    # Miễn trừ theo **hậu tố** chứ không tiền tố: tiền tố dùng được ở đây chỉ có
    # `/api/v1/master/`, và nó sẽ miễn trừ luôn `POST /api/v1/master/{slug}` —
    # đường tạo bản ghi, đúng thứ FR-NFR-004 sinh ra để canh. Hậu tố hẹp tới mức
    # chỉ khớp hai endpoint này và những endpoint nhập liệu ra đời sau chúng.
    "/import/validate",
    "/import/commit",
)
"""Miễn trừ theo **đuôi đường dẫn**, cho nhóm endpoint sinh ra cho từng danh mục.

Cần một dạng thứ ba vì hai dạng trên đều không diễn đạt được nhóm này: liệt kê
đủ thì phải viết ra bốn mươi đường dẫn và cập nhật danh sách ấy mỗi lần phase
sau thêm một danh mục, còn tiền tố thì rộng tới mức nuốt luôn đường ghi."""


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
    return (
        path in IDEMPOTENCY_EXEMPT_PATHS
        or path.startswith(IDEMPOTENCY_EXEMPT_PREFIXES)
        or path.endswith(IDEMPOTENCY_EXEMPT_SUFFIXES)
    )


def route_key_fits_column(route_key: str) -> bool:
    """Khóa route có vừa cột `idempotency_keys.route_key` không.

    Kiểm được **lúc test** (xem test coverage) thay vì lúc chạy: một route quá
    dài sẽ chỉ lộ ra khi có người dùng thật gửi request đầu tiên tới nó.
    """
    column_type = IdempotencyKey.__table__.c.route_key.type
    if not isinstance(column_type, String) or column_type.length is None:
        return True
    return len(route_key) <= column_type.length
