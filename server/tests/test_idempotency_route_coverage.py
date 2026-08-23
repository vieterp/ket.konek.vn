"""Mọi `POST` đổi trạng thái phải khai idempotency — hoặc được miễn tường minh.

Đây là test **chống mục nát**, không phải test hành vi. Quy ước ở plan.md
§Quy ước REST API sẽ đúng ở lát này và sai dần từ phase 6, khi mỗi phân hệ thêm
hàng chục endpoint ghi chứng từ và người viết endpoint thứ bốn mươi chép từ hàng
xóm chứ không đọc lại plan.

Test đọc **bảng route thật** của ứng dụng đã dựng, nên nó thấy đúng những gì
người dùng gọi được — kể cả endpoint mà một phase sau thêm vào rồi quên khai.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from ket.api.idempotency import (
    IDEMPOTENCY_EXEMPT_PATHS,
    IDEMPOTENCY_EXEMPT_PREFIXES,
    IDEMPOTENCY_EXEMPT_SUFFIXES,
    declared_route_key,
    is_exempt,
    iter_api_routes,
    route_key_fits_column,
)
from ket.main import create_app
from ket.settings import Settings


def _post_routes() -> list[APIRoute]:
    app = create_app(Settings(verify_schema_on_startup=False))
    routes = [route for route in iter_api_routes(app.routes) if "POST" in route.methods]
    # Đối chứng cho chính bộ duyệt: nếu FastAPI lại đổi cách biểu diễn router
    # con và `iter_api_routes` không theo kịp, mọi test dưới đây sẽ xanh vì
    # danh sách rỗng thay vì vì mã đúng.
    assert routes, "không duyệt được route POST nào — `iter_api_routes` đã lạc hậu"
    return routes


def test_every_post_route_declares_idempotency_or_is_exempt() -> None:
    """Quên khai `X-Idempotency-Key` ở một endpoint ghi = một chứng từ trùng.

    Sửa test này bằng cách thêm dependency vào endpoint, hoặc — nếu endpoint đó
    thật sự không đổi trạng thái nghiệp vụ — bằng cách thêm tiền tố vào
    `IDEMPOTENCY_EXEMPT_PREFIXES` ở `api/idempotency.py`, nơi người review nhìn
    thấy. **Không** sửa bằng cách nới danh sách trong chính tệp test này.
    """
    missing = [
        route.path
        for route in _post_routes()
        if declared_route_key(route) is None and not is_exempt(route.path)
    ]

    assert not missing, (
        "Endpoint POST chưa khai idempotency (FR-NFR-004): "
        + ", ".join(sorted(missing))
        + ". Thêm `idempotency_key_dependency(...)` vào endpoint, hoặc khai miễn trừ "
        "trong `api/idempotency.py` kèm lý do."
    )


def test_declared_route_keys_fit_the_column() -> None:
    """Khóa route dài quá cột sẽ chỉ lộ ra khi có người dùng thật gọi endpoint."""
    too_long = [
        key
        for key in (declared_route_key(route) for route in _post_routes())
        if key is not None and not route_key_fits_column(key)
    ]

    assert not too_long, f"Khóa route dài quá cột `idempotency_keys.route_key`: {too_long}"


def test_declared_route_keys_are_unique() -> None:
    """Hai endpoint dùng chung một khóa route sẽ chặn nhầm nhau.

    Cùng `route_key` nghĩa là cùng phạm vi khóa: một khóa idempotency đã dùng ở
    endpoint này sẽ bị coi là "đã thực hiện" ở endpoint kia — và endpoint kia im
    lặng không làm gì cả.
    """
    keys = [key for key in (declared_route_key(route) for route in _post_routes()) if key]

    assert len(keys) == len(set(keys)), f"Khóa route bị trùng: {sorted(keys)}"


def test_the_exemption_list_is_exactly_what_was_reviewed() -> None:
    """Danh sách miễn trừ khẳng định **bằng giá trị**, không phải bằng tính chất.

    Đây là chỗ cổng chống-mục-nát dễ bị vô hiệu hóa nhất, và nó **đã** từng bị:
    một đột biến đổi `is_exempt` thành `return True` sống sót qua toàn bộ bộ
    test, vì đối chứng cũ chỉ hỏi "có ít nhất một route khai idempotency không"
    — câu trả lời vẫn đúng sau khi cổng đã tắt.

    Nay mọi lần nới danh sách đều làm test này đỏ, và người sửa phải viết lý do
    vào đúng hai chỗ: hằng số trong mã và khẳng định ở đây.
    """
    assert IDEMPOTENCY_EXEMPT_PREFIXES == (
        "/api/v1/auth/",
        "/api/v1/reports/",
        "/api/v1/jobs/",
    )
    assert IDEMPOTENCY_EXEMPT_PATHS == frozenset(
        {
            "/api/v1/system/users/{user_id}/roles",
            "/api/v1/system/users/{user_id}/branches",
            "/api/v1/jobs",
            # Lát 5D: in chứng từ — in lần 2 là sự kiện thật mà FR-RPT-011 đếm
            # (`copy_no` + cảnh báo), khử trùng lặp sẽ nói dối số lần in.
            "/api/v1/vouchers/{voucher_id}/print",
            # Lát 6E-2: in biên bản kiểm kê quỹ — không ghi gì cả, kể cả
            # `print_log` (biên bản không phải chứng từ). POST chỉ vì trả tệp.
            "/api/v1/cash-book/count-sheets/{sheet_id}/print",
            # Lát 5E: nút một-bước gán logo — gửi lại cùng tệp ghi lại cùng
            # hash và cùng hai giá trị settings; kho blob content-addressed
            # khử trùng theo nội dung nên không có gì bị nhân đôi.
            "/api/v1/system/settings/logo",
            # Lát 6D: nhập sao kê — khóa băm-nội-dung per-TK chặn nhân đôi
            # (409 duplicate), bền hơn khóa idempotency vì không hết hạn.
            "/api/v1/bank/statements/import",
            # Lát 6D: ba thao tác khớp ghi trạng thái của một tập dòng (cùng
            # họ gán vai trò): chạy lại cho ra đúng trạng thái đó hoặc 409,
            # không lần gửi lại nào tạo thêm bản ghi.
            "/api/v1/bank/statements/{statement_id}/actions/auto-match",
            "/api/v1/bank/statements/lines/{line_id}/actions/match",
            "/api/v1/bank/statements/lines/{line_id}/actions/unmatch",
        }
    )
    # Cơ chế thứ ba, thêm ở lát 3C-1. Ghim nó ở đây vì chính docstring bên trên
    # hứa "mọi lần nới danh sách đều làm test này đỏ" — và lời hứa ấy **đã sai**
    # trong khoảng thời gian hằng số này tồn tại mà khẳng định thì chưa: thêm
    # `"/warehouses"` vào tuple hậu tố miễn trừ luôn `POST /api/v1/master/warehouses`,
    # đúng đường tạo bản ghi mà FR-NFR-004 sinh ra để canh, và không test nào đỏ.
    assert IDEMPOTENCY_EXEMPT_SUFFIXES == (
        "/import/validate",
        "/import/commit",
    )


def test_the_exemption_list_does_not_cover_everything() -> None:
    """Ít nhất một route phải **thật sự** khai idempotency.

    Giữ lại như lưới thứ hai: nếu một lần dọn dẹp nào đó gỡ hết dependency khỏi
    mọi endpoint, test trên vẫn xanh (danh sách miễn trừ không đổi) còn test này
    thì đỏ.
    """
    declared = [route.path for route in _post_routes() if declared_route_key(route) is not None]

    assert declared, "không endpoint nào khai idempotency — cổng coverage đã rỗng"


def test_an_exempt_prefix_does_not_leak_to_a_similarly_named_path() -> None:
    """`"/api/v1/jobs"` thiếu dấu `/` cuối sẽ miễn trừ luôn `/api/v1/jobsomething`.

    Endpoint ghi của một phase sau chỉ cần trùng tiền tố là lọt qua cổng mà
    không ai thấy.
    """
    assert is_exempt("/api/v1/jobs"), "đường xếp job vẫn phải được miễn trừ"
    assert is_exempt("/api/v1/jobs/{job_id}/cancel")
    assert not is_exempt("/api/v1/jobsomething")
    assert not is_exempt("/api/v1/authorizations")
