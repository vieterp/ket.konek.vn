"""Hạn mức request theo người gọi.

Không cần PostgreSQL: mọi thứ được kiểm ở đây xảy ra **trước** khi request chạm
tầng dịch vụ — đó chính là lý do hạn mức là middleware. Dùng `/health` và các
endpoint trả `401` để đo, vì chúng không mở transaction nào.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ket.api.middleware.problem_details import PROBLEM_CONTENT_TYPE
from ket.api.middleware.rate_limit import IP_BUDGET_FACTOR, MAX_TRACKED_CALLERS
from ket.api.middleware.request_context import CORRELATION_HEADER
from ket.main import create_app
from ket.settings import Settings


def _client(**overrides: int) -> Iterator[TestClient]:
    settings = Settings(
        # Tắt cả hai cổng khởi động: chúng cần một PostgreSQL thật, còn hạn mức
        # thì chạy trước khi request chạm tới bất kỳ connection nào.
        verify_schema_on_startup=False,
        verify_postgres_version_on_startup=False,
        **overrides,
    )
    app = create_app(settings)
    # Không vào `lifespan`: nó dựng pool và kiểm phiên bản schema, hai việc cần
    # DB. Hạn mức chạy trước cả hai.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def tight_client() -> Iterator[TestClient]:
    """Hạn mức thấp để đếm được bằng vài request."""
    yield from _client(rate_limit_per_minute=3, rate_limit_auth_per_minute=2)


def test_requests_beyond_the_limit_get_429_with_retry_after(tight_client: TestClient) -> None:
    statuses = [tight_client.get("/api/v1/system/datasets").status_code for _ in range(5)]

    assert statuses[:3] == [401, 401, 401], "ba request đầu phải đi tới tầng xác thực"
    assert statuses[3:] == [429, 429]

    blocked = tight_client.get("/api/v1/system/datasets")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    body = blocked.json()
    assert body["error_code"] == "system.rate_limited"
    assert body["details"]["limit"] == 3


def test_a_rate_limited_response_is_still_problem_json_with_a_correlation_id(
    tight_client: TestClient,
) -> None:
    """Khóa thứ tự middleware: hạn mức phải nằm **trong** lớp mã tương quan.

    Đảo thứ tự trong `create_app` thì phản hồi `429` mất `correlation_id` và
    người dùng gặp lỗi không còn gì để đọc cho bộ phận hỗ trợ. Ở đây chỉ một
    dòng test giữ điều đó.
    """
    for _ in range(4):
        response = tight_client.get("/api/v1/system/datasets")

    assert response.status_code == 429
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.headers[CORRELATION_HEADER]
    assert response.json()["correlation_id"] is not None


def test_the_auth_group_has_its_own_tighter_budget(tight_client: TestClient) -> None:
    """Dò mật khẩu không được tiêu ngân sách chung, và ngược lại."""
    payload = {"username": "khong_co_that", "password": "Sai#Mat0Khau"}

    first = tight_client.post("/api/v1/auth/login", json=payload)
    second = tight_client.post("/api/v1/auth/login", json=payload)
    third = tight_client.post("/api/v1/auth/login", json=payload)

    # Hai request đầu đi qua hạn mức (rồi hỏng vì không có DB — không sao, cổng
    # hạn mức nằm trước đó); request thứ ba chạm trần **riêng** của nhóm auth.
    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert third.json()["details"]["limit"] == 2

    # Ngân sách chung vẫn còn nguyên: hai nhóm đếm riêng.
    assert tight_client.get("/api/v1/system/datasets").status_code == 401


def test_health_is_never_rate_limited(tight_client: TestClient) -> None:
    """Trình giám sát gọi `/health` theo vòng lặp — chặn nó là báo động giả."""
    statuses = {tight_client.get("/health").status_code for _ in range(10)}

    assert statuses == {200}


def test_a_zero_limit_turns_the_gate_off() -> None:
    """`0` = tắt. Cần cho diễn tập tải và cho lúc gỡ rối."""
    for client in _client(rate_limit_per_minute=0, rate_limit_auth_per_minute=0):
        statuses = {client.get("/api/v1/system/datasets").status_code for _ in range(20)}
        assert statuses == {401}


def test_two_callers_do_not_share_a_budget() -> None:
    """Một client hỏng không được kéo cả văn phòng xuống theo.

    Chỉ kiểm **đã bị chặn hay chưa**, không kiểm mã trạng thái của request đi
    lọt: có token nghĩa là request chạm tầng tra phiên, mà nhóm test này cố ý
    chạy không có PostgreSQL. Thứ đang được kiểm nằm trước đó — hai token khác
    nhau phải là hai ngân sách khác nhau.
    """
    for client in _client(rate_limit_per_minute=2, rate_limit_auth_per_minute=2):
        noisy = {"Authorization": "Bearer token-cua-may-hong"}
        quiet = {"Authorization": "Bearer token-cua-nguoi-khac"}

        assert client.get("/api/v1/system/datasets", headers=noisy).status_code != 429
        assert client.get("/api/v1/system/datasets", headers=noisy).status_code != 429
        assert client.get("/api/v1/system/datasets", headers=noisy).status_code == 429

        assert client.get("/api/v1/system/datasets", headers=quiet).status_code != 429


def test_a_forged_authorization_header_cannot_buy_a_fresh_budget() -> None:
    """Định danh ngân sách không được lấy từ thứ người gọi tự khai.

    Bản đầu tiên của middleware khóa theo băm header `Authorization`, nên
    `Bearer rac-1`, `Bearer rac-2`, … là mỗi request một ngân sách mới và trần
    không bao giờ chạm tới. Đó là **đúng** kịch bản mà tệp này tuyên bố chặn:
    một máy trạm nhiễm mã độc rải đều lần thử qua nhiều tài khoản, nơi khóa
    theo tài khoản (lát 2B-1a) không giúp gì.
    """
    for client in _client(rate_limit_per_minute=3, rate_limit_auth_per_minute=2):
        statuses = [
            client.post(
                "/api/v1/auth/login",
                json={"username": f"nan_nhan_{index}", "password": "Sai#Mat0Khau"},
                headers={"Authorization": f"Bearer rac-{index}"},
            ).status_code
            for index in range(50)
        ]

    assert 429 in statuses, f"50 lần thử đăng nhập không lần nào bị chặn: {set(statuses)}"
    assert statuses.count(429) > 40, "chặn được nhưng quá muộn — ngân sách vẫn bị nhân lên"


def test_forged_identities_cannot_evict_a_real_caller_budget() -> None:
    """Bơm định danh giả không được trở thành công tắc tắt hạn mức.

    Bản đầu tiên `clear()` sạch bảng khi chạm trần, nên đẩy bảng qua trần là đặt
    lại bộ đếm của **mọi** người dùng thật. Nay bảng đầy thì loại cửa sổ cũ
    nhất, và kẻ bơm tự đẩy chính mình ra vì mỗi định danh của nó chỉ dùng đúng
    một lần.
    """
    for client in _client(rate_limit_per_minute=2, rate_limit_auth_per_minute=2):
        real = {"Authorization": "Bearer token-nguoi-that"}
        for _ in range(3):
            blocked = client.get("/api/v1/system/datasets", headers=real)
        assert blocked.status_code == 429, "người thật phải bị chặn trước khi bơm"

        for index in range(MAX_TRACKED_CALLERS + 200):
            client.get("/api/v1/system/datasets", headers={"Authorization": f"Bearer rac-{index}"})

        assert client.get("/api/v1/system/datasets", headers=real).status_code == 429, (
            "bộ đếm của người thật bị xóa sạch bởi định danh giả"
        )


def test_one_machine_cannot_exceed_the_ip_ceiling_by_splitting_tokens() -> None:
    """Token chỉ **chia nhỏ** ngân sách của một IP, không nhân nó lên."""
    limit = 2
    for client in _client(rate_limit_per_minute=limit, rate_limit_auth_per_minute=limit):
        statuses = [
            client.get(
                "/api/v1/system/datasets", headers={"Authorization": f"Bearer token-{index}"}
            ).status_code
            for index in range(limit * IP_BUDGET_FACTOR + 5)
        ]

    assert 429 in statuses, f"trần theo IP không có tác dụng: {set(statuses)}"
