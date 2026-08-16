"""Hợp đồng HTTP của `/api/v1/auth` và định dạng lỗi RFC 7807 (bước 12).

Test ở tầng này kiểm những thứ mà test dịch vụ không thấy: mã HTTP, kiểu nội
dung, hình dạng thân lỗi, header mã tương quan — tức là chính xác những gì client
Tauri/React sẽ lập trình dựa vào. Đổi bất kỳ thứ nào trong đó là **breaking
change** với client, nên chúng cần một chỗ bị khóa lại.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.middleware.problem_details import PROBLEM_CONTENT_TYPE
from ket.api.middleware.request_context import CORRELATION_HEADER
from ket.kernel.datasets.models import User
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """App thật chạy qua `lifespan` thật, trên database của bộ fixture.

    `raise_server_exceptions=False` là bắt buộc cho test dưới cùng tệp: mặc định
    `TestClient` ném lại ngoại lệ của server thay vì trả phản hồi, nên handler
    500 — thứ đang cần kiểm — sẽ không bao giờ chạy.

    `app_engine`/`session_factory` nhận vào để thứ tự dựng fixture đúng: cả hai
    kéo theo việc dựng database test trước khi app khởi động.
    """
    assert app_engine is not None and session_factory is not None
    app = create_app(test_settings)
    with api_test_client(app) as instance:
        yield instance


def _login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}


def test_login_returns_a_token_and_expiry(client: TestClient, user_factory: UserFactory) -> None:
    user = user_factory("api")
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["expires_at"]
    assert body["must_change_password"] is False


def test_wrong_password_is_a_problem_document_not_a_plain_error(
    client: TestClient, user_factory: UserFactory
) -> None:
    """Hình dạng thân lỗi là hợp đồng: client dựng câu tiếng Việt từ `error_code`."""
    user = user_factory("api_sai")
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": "Sai#MatKhau2026"}
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert body["error_code"] == "auth.invalid_credentials"
    assert body["status"] == 401
    assert body["correlation_id"]


def test_missing_or_malformed_token_is_rejected_the_same_way(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}):
        response = client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["error_code"] == "auth.not_authenticated"


def test_a_token_with_non_ascii_characters_is_a_401_not_a_500(client: TestClient) -> None:
    """Dữ liệu ngoài không được biến thành lỗi máy chủ — xem `token_digest`.

    Header HTTP đi trên dây dưới dạng latin-1, nên giá trị phải gửi dạng bytes:
    một chuỗi Python có dấu bị chính httpx từ chối trước khi ra khỏi máy, và test
    sẽ "xanh" mà chưa hề chạm tới server.
    """
    response = client.get(
        "/api/v1/auth/me", headers={b"Authorization": "Bearer tkn-\xe9\xf4".encode("latin-1")}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "auth.not_authenticated"


def test_me_returns_the_current_identity(client: TestClient, user_factory: UserFactory) -> None:
    user = user_factory("api_me")
    response = client.get("/api/v1/auth/me", headers=_login(client, user.username))

    assert response.status_code == 200
    assert response.json()["username"] == user.username


def test_logout_makes_the_token_unusable(client: TestClient, user_factory: UserFactory) -> None:
    user = user_factory("api_thoat")
    headers = _login(client, user.username)

    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_change_password_requires_the_current_one(
    client: TestClient, user_factory: UserFactory
) -> None:
    user = user_factory("api_doi")
    headers = _login(client, user.username)

    wrong = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "Sai#MatKhau2026", "new_password": "Moi#MatKhau2026"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["error_code"] == "auth.invalid_credentials"


def test_change_password_enforces_the_policy_and_names_the_rule(
    client: TestClient, user_factory: UserFactory
) -> None:
    user = user_factory("api_yeu")
    response = client.post(
        "/api/v1/auth/change-password",
        headers=_login(client, user.username),
        json={"current_password": PASSWORD, "new_password": "ngan"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "auth.password_too_weak"
    assert body["details"]["rule"] == "min_length"


def test_change_password_switches_the_credentials(
    client: TestClient, user_factory: UserFactory
) -> None:
    user = user_factory("api_doixong")
    response = client.post(
        "/api/v1/auth/change-password",
        headers=_login(client, user.username),
        json={"current_password": PASSWORD, "new_password": "Moi#MatKhau2026"},
    )
    assert response.status_code == 204

    stale = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert stale.status_code == 401
    assert _login(client, user.username, "Moi#MatKhau2026")


def test_malformed_body_is_a_validation_problem(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "chỉ-có-tên"})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "request.validation_failed"
    assert body["errors"]


def test_correlation_id_is_echoed_back(client: TestClient) -> None:
    given = str(uuid4())
    response = client.get("/health", headers={CORRELATION_HEADER: given})
    assert response.headers[CORRELATION_HEADER] == given


def test_a_garbage_correlation_id_does_not_break_the_request(client: TestClient) -> None:
    """Header lần vết hỏng không được làm hỏng nghiệp vụ — nó chỉ là để tra log."""
    response = client.get("/health", headers={CORRELATION_HEADER: "khong-phai-uuid"})
    assert response.status_code == 200
    assert response.headers[CORRELATION_HEADER] != "khong-phai-uuid"


def test_unexpected_errors_never_leak_internals(client: TestClient) -> None:
    """500 chỉ được trả mã tham chiếu — không traceback, không thông điệp thô.

    Endpoint nổ được cài thẳng vào app để kiểm chính handler, thay vì chờ một lỗi
    thật xuất hiện ở đâu đó rồi mới biết nó lộ những gì.
    """
    secret = "chi-tiết-nội-bộ-không-được-lộ"

    @client.app.get("/test-only/no-bao-gio")  # type: ignore[union-attr]
    def _explode() -> None:
        raise RuntimeError(secret)

    response = client.get("/test-only/no-bao-gio")

    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == "system.internal_error"
    assert body["correlation_id"]
    assert secret not in response.text
    assert "Traceback" not in response.text
