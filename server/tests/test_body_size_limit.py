"""Trần thân request chặn **trước** khi thân chạm đĩa (C1, FR-NFR-053).

Không cần PostgreSQL, và điều đó chính là nội dung đang kiểm: một request bị từ
chối ở đây chưa đi tới tầng nào khác — chưa xác thực, chưa phân quyền, chưa
dependency nào chạy.

Vì sao phải có tệp này thay vì tin vào trần trong `storage.store_stream`: FastAPI
gọi `await request.form()` **trước** `solve_dependencies`, và
`MultiPartParser` đổ phần tệp vào `SpooledTemporaryFile` không trần. Trước lát
này, một request **không mang token** đẩy được 12 MiB xuống thư mục tạm của một
bản cài đặt trần 1 KiB. Trên bản cài một máy, thư mục tạm dùng chung ổ với
PostgreSQL.

Bất biến then chốt của tệp: **bộ phân tích multipart không bao giờ được chạy**
cho một thân vượt trần. Test dưới đây kiểm đúng điều đó bằng cách theo dõi chính
`MultiPartParser.parse` — chứ không kiểm gián tiếp qua mã trạng thái, thứ vẫn
đúng ngay cả khi đĩa đã bị ghi đầy.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette import formparsers

from ket import __version__
from ket.api.middleware.body_size_limit import (
    DEFAULT_MAX_BODY_BYTES,
    MULTIPART_OVERHEAD_BYTES,
)
from ket.api.middleware.schema_version_gate import CLIENT_VERSION_HEADER
from ket.main import create_app
from ket.settings import Settings

ATTACHMENT_LIMIT = 4096
"""Trần tệp của bản cài dùng trong tệp test này."""


@pytest.fixture
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    settings = Settings(
        verify_schema_on_startup=False,
        verify_postgres_version_on_startup=False,
        attachments_dir=tmp_path_factory.mktemp("attachments"),
        attachment_max_bytes=ATTACHMENT_LIMIT,
    )
    # **Không** vào `lifespan`: request bị trần chặn phải dừng trước cả khi có ai
    # cần tới pool kết nối. Nếu một ngày nào đó nó cần, test sẽ đỏ với `500` —
    # đúng tín hiệu, vì khi ấy trần đã tụt xuống sau tầng dữ liệu.
    yield TestClient(
        create_app(settings),
        raise_server_exceptions=False,
        headers={CLIENT_VERSION_HEADER: __version__},
    )


@pytest.fixture
def parser_spy(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Ghi lại việc bộ phân tích multipart có được chạy hay không."""
    calls: list[bool] = []
    original = formparsers.MultiPartParser.parse

    async def spy(self: formparsers.MultiPartParser) -> formparsers.FormData:
        calls.append(True)
        return await original(self)

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", spy)
    return calls


def test_an_oversized_upload_is_refused_before_anything_touches_the_disk(
    client: TestClient, parser_spy: list[bool]
) -> None:
    """Kẻ gọi **chưa đăng nhập** không ghi được một byte nào lên máy chủ."""
    payload = b"x" * (ATTACHMENT_LIMIT + MULTIPART_OVERHEAD_BYTES + 1)

    response = client.post(
        "/api/v1/attachments",
        data={"entity_type": "vouchers", "entity_id": "1"},
        files={"file": ("qua-lon.bin", payload, "application/octet-stream")},
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "request.body_too_large"
    assert parser_spy == [], "thân vượt trần vẫn được phân tích — tức là đã ghi xuống đĩa"


def test_the_refusal_needs_no_authentication_and_no_database(client: TestClient) -> None:
    """Không token, không dataset, không PostgreSQL — vẫn phải là `413`.

    Nếu phép kiểm này tụt xuống sau tầng xác thực, mã trạng thái sẽ thành `401`
    và ổ đĩa đã bị ghi trước đó.
    """
    response = client.post(
        "/api/v1/attachments",
        data={"entity_type": "vouchers", "entity_id": "1"},
        files={"file": ("qua-lon.bin", b"y" * (ATTACHMENT_LIMIT * 4), "application/octet-stream")},
    )

    assert response.status_code == 413


def test_a_client_that_lies_about_content_length_is_still_stopped(
    client: TestClient, parser_spy: list[bool]
) -> None:
    """Không có `Content-Length` (truyền theo lô) thì trần vẫn phải đếm được.

    Từ chối sớm theo header là **đường tắt cho client trung thực**. Phép đo thật
    là số byte đếm được trong lúc nhận — nếu không, mọi kẻ gọi chỉ cần bỏ header
    đi là qua được cổng.

    Khác trường hợp có `Content-Length` ở chỗ: không khai trước thì không có cách
    nào biết trước, nên bộ phân tích **có** bắt đầu chạy. Điều được bảo đảm là nó
    bị cắt ở đúng trần — số byte chạm đĩa bị chặn trên, thay vì không có trần nào
    như trước lát này.
    """

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"z" * 4096

    response = client.post(
        "/api/v1/attachments",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=gia-dinh"},
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "request.body_too_large"
    assert len(parser_spy) <= 1, "mỗi request chỉ được phân tích một lần"


def test_a_json_endpoint_keeps_the_small_default_limit(client: TestClient) -> None:
    """Trần rộng chỉ dành cho đường tải tệp, không cho cả API.

    Một trần chung nới cho vừa tệp đính kèm sẽ cho **mọi** endpoint quyền nhận
    25 MiB — đổi một lỗ lấy một lỗ rộng hơn.
    """
    response = client.post(
        "/api/v1/auth/login",
        content=b"{" + b"a" * (DEFAULT_MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "request.body_too_large"


def test_a_request_within_the_limit_passes_through(
    client: TestClient, parser_spy: list[bool]
) -> None:
    """Đối chứng: cổng này không được chặn nhầm request bình thường.

    Kiểm bằng chính bộ phân tích multipart — nó **phải** chạy — thay vì bằng mã
    trạng thái: app trong tệp này cố ý không vào `lifespan`, nên request đi qua
    được trần sẽ dừng ở tầng dữ liệu chứ không ở tầng xác thực. Điều đang kiểm là
    cổng dung lượng, không phải nơi request dừng lại sau đó.
    """
    response = client.post(
        "/api/v1/attachments",
        data={"entity_type": "vouchers", "entity_id": "1"},
        files={"file": ("vua-du.bin", b"x" * 512, "application/octet-stream")},
    )

    assert response.status_code != 413
    assert parser_spy == [True], "request trong trần phải được đọc bình thường"
