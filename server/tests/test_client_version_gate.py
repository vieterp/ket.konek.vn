"""Khóa phiên bản client ↔ server (bước 19, LD-05, FR-NFR-054).

Không cần PostgreSQL: thứ được kiểm là **middleware**, chạy trước cả tầng xác
thực. Request nào lọt qua cổng sẽ chạm một engine nổ tại chỗ (xem `_client`),
đúng khuôn của `test_rate_limit.py` và cùng lý do — không được để test mở kết
nối mạng thật.

Bất biến mà tệp này canh, theo thứ tự tầm quan trọng:

1. Client cũ **không ghi** được, nhưng **đọc** được. Chế độ chỉ-đọc là thứ khiến
   cổng này an toàn để bật; mất nó thì một lần tăng `min_client_version` nhầm
   làm cả văn phòng dừng việc.
2. Thiếu header thì **chặn** (H2). Đây là bất biến dễ bị nới nhất khi một script
   vận hành nào đó vấp phải nó, và nới ra thì cổng chỉ còn chặn được client
   trung thực.
3. Đăng nhập và đăng xuất **không** bị chặn — nếu không, "chỉ-đọc" là chữ trên
   giấy: không có phiên thì không đọc được gì.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ket import __version__
from ket.api.dependencies import get_session_factory
from ket.api.middleware.problem_details import PROBLEM_CONTENT_TYPE
from ket.api.middleware.schema_version_gate import CLIENT_VERSION_HEADER, EXEMPT_PATHS
from ket.kernel.errors import ClientVersionUnsupportedError
from ket.main import create_app
from ket.settings import Settings

MINIMUM = "1.4.0"
"""Bản tối thiểu dùng trong test — cố ý **khác** phiên bản đang phát hành, để
không test nào xanh chỉ vì hai con số tình cờ bằng nhau."""

WRITE_PATH = "/api/v1/system/branches"
"""Một đường ghi thật, có kiểm quyền — không phải endpoint dựng riêng cho test.

Quan trọng: nó nằm **sau** cả xác thực lẫn phân quyền, nên khi cổng phiên bản
cho đi tiếp, test thấy `401` chứ không thấy `2xx`. Đó chính là tín hiệu cần —
"đã qua cổng" — mà không cần dựng một phiên đăng nhập thật.
"""

READ_PATH = "/api/v1/system/datasets"


def _refuse_to_connect() -> NoReturn:
    raise RuntimeError("nhóm test cổng phiên bản không được chạm DB")


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        verify_schema_on_startup=False,
        verify_postgres_version_on_startup=False,
        minimum_client_version=MINIMUM,
        # Hạn mức tắt: test này gửi hàng chục request trong một cửa sổ và
        # không có gì để nói về hạn mức.
        rate_limit_per_minute=0,
        rate_limit_auth_per_minute=0,
    )
    app = create_app(settings)
    broken = create_engine(settings.database_url, creator=_refuse_to_connect)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=broken)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _post(client: TestClient, version: str | None, path: str = WRITE_PATH) -> Response:
    headers = {} if version is None else {CLIENT_VERSION_HEADER: version}
    return client.post(path, json={}, headers=headers)


# --- Chặn ------------------------------------------------------------------


def test_an_older_client_cannot_write(client: TestClient) -> None:
    response = client.post(WRITE_PATH, json={}, headers={CLIENT_VERSION_HEADER: "1.3.9"})

    assert response.status_code == 426
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert body["error_code"] == ClientVersionUnsupportedError.error_code
    assert body["details"] == {"client_version": "1.3.9", "min_client_version": MINIMUM}


def test_a_request_without_the_header_cannot_write(client: TestClient) -> None:
    """Quyết định H2, và là bất biến dễ bị nới nhất của tệp này.

    Cổng cho qua khi vắng header chỉ chặn được client trung thực: bản cũ chỉ cần
    **bỏ** header là đi vòng qua đúng cơ chế sinh ra để chặn nó.
    """
    response = client.post(WRITE_PATH, json={})

    assert response.status_code == 426
    body = response.json()
    assert body["details"]["client_version"] is None
    assert CLIENT_VERSION_HEADER in body["detail"], "thông điệp phải nêu đích danh header còn thiếu"


@pytest.mark.parametrize(
    "raw", ["v1.4.0", "1.4", "1.4.0-beta", "1.4.0.1", "", "  ", "latest", "99999.0.0"]
)
def test_a_malformed_version_is_treated_as_unsupported(client: TestClient, raw: str) -> None:
    """Không đọc được ≠ đọc được rồi thấy mới.

    Một chuỗi lạ có thể là client hỏng, có thể là ai đó đang thử đi vòng. Cả hai
    đều không phải bản client đã kiểm được, nên cả hai đi cùng một cửa.
    """
    response = client.post(WRITE_PATH, json={}, headers={CLIENT_VERSION_HEADER: raw})

    assert response.status_code == 426


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_write_method_is_gated(client: TestClient, method: str) -> None:
    """Không chỉ `POST`: sửa và xóa cũng là ghi vào sổ."""
    call = getattr(client, method)
    response = call("/api/v1/system/branches/1")

    assert response.status_code == 426


# --- Cho qua ---------------------------------------------------------------


def test_the_matching_version_passes_the_gate(client: TestClient) -> None:
    response = client.post(WRITE_PATH, json={}, headers={CLIENT_VERSION_HEADER: MINIMUM})

    assert response.status_code == 401, "qua cổng phiên bản thì dừng ở xác thực"


def test_a_newer_client_passes_the_gate(client: TestClient) -> None:
    """Client mới hơn server **không** bị server chặn.

    Nhánh này thường có nghĩa là máy trạm vừa tự cập nhật trước máy chủ, và việc
    phải làm nằm ở máy chủ. Client tự hiện cảnh báo từ `/system/handshake`;
    chặn ghi ở đây chỉ làm người dùng mất việc đang gõ dở mà không sửa được gì.
    """
    response = client.post(WRITE_PATH, json={}, headers={CLIENT_VERSION_HEADER: "2.0.0"})

    assert response.status_code == 401


def test_version_ordering_is_numeric_not_lexicographic(client: TestClient) -> None:
    """`0.10.0` mới hơn `0.9.0` — so chuỗi sẽ trả lời ngược."""
    numeric = create_app(
        Settings(
            verify_schema_on_startup=False,
            verify_postgres_version_on_startup=False,
            minimum_client_version="0.9.0",
        )
    )
    broken = create_engine("postgresql+psycopg://ket_app@localhost/ket", creator=_refuse_to_connect)
    numeric.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=broken)

    with TestClient(numeric, raise_server_exceptions=False) as test_client:
        response = test_client.post(WRITE_PATH, json={}, headers={CLIENT_VERSION_HEADER: "0.10.0"})

    assert response.status_code == 401


def test_reads_still_work_for_an_old_client(client: TestClient) -> None:
    """Chế độ chỉ-đọc (FR-NFR-054): tra cứu sổ sách không bị chặn."""
    response = client.get(READ_PATH, headers={CLIENT_VERSION_HEADER: "0.0.1"})

    assert response.status_code == 401, "dừng ở xác thực, không phải ở cổng phiên bản"


def test_reads_without_the_header_still_work(client: TestClient) -> None:
    response = client.get(READ_PATH)

    assert response.status_code == 401


def test_login_is_exempt(client: TestClient) -> None:
    """Chỉ-đọc cần một phiên, mà phiên thì phải đăng nhập mới có.

    Chặn `login` biến "chỉ-đọc" thành "không dùng được gì" — tức là xóa mất
    chính cơ chế giữ cho cổng này an toàn khi bật.
    """
    response = _post(client, None, path="/api/v1/auth/login")

    assert response.status_code != 426


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/change-password",
        "/api/v1/auth/totp/enroll",
        "/api/v1/auth/totp/confirm",
    ],
)
def test_account_recovery_paths_are_exempt(client: TestClient, path: str) -> None:
    """Chỉ-đọc phải có thật cho **mọi** tài khoản, không chỉ tài khoản đã yên vị.

    Tài khoản mang mật khẩu tạm, hoặc thuộc diện bắt buộc 2FA mà chưa đăng ký
    thiết bị, không đọc được gì cả — `get_current_principal` đóng cả hai trạng
    thái. Chặn thêm ba đường này thì hai loại tài khoản đó bị **khóa cứng**, và
    một bản cài mới (nơi mọi tài khoản đều mang mật khẩu tạm) sẽ không ai vào
    được. Đo được trên bản cài thật ở review lát 2C-1.
    """
    response = client.post(path, json={})

    assert response.status_code != 426


def test_the_exemption_list_is_exactly_five_paths() -> None:
    """Danh sách đóng: mỗi mục thêm vào là một đường ghi mà client cũ đi qua được.

    Khóa cả tập hợp chứ không chỉ từng mục — thêm một đường mới phải là hành
    động có chủ đích, đi kèm việc sửa test này và nêu lý do.
    """
    assert EXEMPT_PATHS == frozenset(
        {
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/change-password",
            "/api/v1/auth/totp/enroll",
            "/api/v1/auth/totp/confirm",
        }
    )


def test_logout_is_exempt(client: TestClient) -> None:
    """Thu hồi phiên phải chạy được ở mọi trạng thái, kể cả từ bản client cũ."""
    response = client.post("/api/v1/auth/logout", headers={CLIENT_VERSION_HEADER: "0.0.1"})

    assert response.status_code == 401, "dừng ở thiếu token, không phải ở cổng phiên bản"


# --- Bắt tay ---------------------------------------------------------------


def test_handshake_is_anonymous_and_reports_both_versions(client: TestClient) -> None:
    """Client gọi trước khi đăng nhập, nên endpoint này không được đòi token."""
    response = client.get("/api/v1/system/handshake")

    assert response.status_code == 200
    body = response.json()
    assert body["server_version"] == __version__
    assert body["min_client_version"] == MINIMUM
    assert body["deployment_mode"] == "standalone"
    assert body["control_schema_version"].isdigit()


def test_handshake_does_not_leak_installation_details(client: TestClient) -> None:
    """Endpoint ẩn danh trong LAN — mỗi trường thêm vào là thứ ai cũng đọc được.

    Khóa danh sách trường lại bằng test: thêm tên doanh nghiệp hay danh sách dữ
    liệu kế toán vào đây là việc rất dễ làm ở một lát sau, khi màn hình đăng
    nhập cần "tiện" hiện tên công ty.
    """
    body = client.get("/api/v1/system/handshake").json()

    assert set(body) == {
        "server_version",
        "min_client_version",
        "control_schema_version",
        "deployment_mode",
    }


def test_the_default_minimum_is_the_server_version() -> None:
    """Bản cài chưa ai chỉnh cấu hình đòi client cùng lứa.

    Hướng mặc định phải là siết: nới ra là quyết định có chủ đích của người
    triển khai, còn siết vào là thứ họ sẽ quên làm.
    """
    assert Settings().minimum_client_version == __version__


def test_a_malformed_minimum_in_config_refuses_to_start() -> None:
    """Cấu hình gõ sai phải chặn tiến trình, không được im lặng thành vô hiệu."""
    with pytest.raises(ValueError, match=r"MAJOR\.MINOR\.PATCH"):
        Settings(minimum_client_version="mới nhất")


# --- CORS ------------------------------------------------------------------
#
# Không phải "một tính năng" mà là điều kiện để client desktop chạy được: webview
# của Tauri có origin riêng (`tauri://localhost`), nên mọi lời gọi của nó là
# xuyên origin. Thiếu lớp này, triệu chứng là đăng nhập không phản hồi trong khi
# log máy chủ hoàn toàn trống — trình duyệt chặn trước khi request rời máy.


def test_the_tauri_origin_may_call_the_api(client: TestClient) -> None:
    response = client.get("/api/v1/system/handshake", headers={"Origin": "tauri://localhost"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_preflight_is_answered_with_every_header_the_client_sends(client: TestClient) -> None:
    """Preflight phải qua **trước** hạn mức và cổng phiên bản.

    Nó không mang token và không đổi gì; chặn nó chỉ làm trình duyệt báo "lỗi
    mạng" thay vì báo đúng lý do từ chối.
    """
    response = client.options(
        "/api/v1/system/branches",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                f"authorization,content-type,{CLIENT_VERSION_HEADER.lower()},x-dataset,"
                "x-idempotency-key"
            ),
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "x-dataset", "x-idempotency-key", "x-client-version"):
        assert header in allowed


def test_an_unknown_origin_is_not_allowed(client: TestClient) -> None:
    """Danh sách đóng, không `*`: hệ này giữ PII lương và bí mật doanh nghiệp."""
    response = client.get(
        "/api/v1/system/handshake", headers={"Origin": "https://ke-la-mat.example"}
    )

    assert "access-control-allow-origin" not in response.headers


def test_the_public_error_code_string_is_frozen(client: TestClient) -> None:
    """Chuỗi mã lỗi là **hợp đồng công khai** — khẳng định chính chuỗi đó.

    Khẳng định `== ClientVersionUnsupportedError.error_code` là so một hằng với
    chính nó: đổi tên mã ở lớp lỗi thì test vẫn xanh, trong khi client khai
    chuỗi cứng trong hai catalog ngôn ngữ và sẽ âm thầm rơi về "lỗi không xác
    định". Đo được ở review lát 2C-1 (đột biến M15 sống sót cả 293 test).
    """
    body = client.post(WRITE_PATH, json={}).json()

    assert body["error_code"] == "system.client_version_unsupported"
    assert body["type"] == "https://konek.vn/errors/system.client_version_unsupported"


def test_a_rejected_write_still_carries_cors_headers(client: TestClient) -> None:
    """CORS phải bọc **ngoài** cổng phiên bản, không phải trong.

    Nếu lớp CORS nằm trong, phản hồi `426` tới trình duyệt mà thiếu
    `access-control-allow-origin`, và webview Tauri hiện "lỗi mạng" thay vì màn
    hình cập nhật — đúng thứ mà thứ tự middleware sinh ra để tránh. Đảo thứ tự
    hai lớp này **không** làm đỏ test nào khác (đo ở review, đột biến M13).
    """
    response = client.post(WRITE_PATH, json={}, headers={"Origin": "tauri://localhost"})

    assert response.status_code == 426
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_exemptions_match_the_whole_path_not_a_prefix(client: TestClient) -> None:
    """Miễn trừ so khớp **đúng chuỗi**, không phải tiền tố.

    Hôm nay chưa có route ghi nào nằm dưới `/auth/login`, nên khớp theo tiền tố
    chưa thành lỗ hổng — nhưng danh sách này là chỗ **duy nhất** một lệnh ghi đi
    qua cổng, và một route thêm sau ở phase 7 sẽ biến nó thành lỗ hổng mà không
    ai nhận ra.
    """
    response = client.post("/api/v1/auth/login/them-duong-nao-do", json={})

    assert response.status_code == 426
