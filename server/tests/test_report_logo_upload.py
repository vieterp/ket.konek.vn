"""Nút một-bước gán logo báo cáo (`POST /api/v1/system/settings/logo`, lát 5E).

Trước lát này gán logo là hai việc tay (tải tệp + dán hash vào setting) — nợ
(d) của bàn giao 5D. Bất biến ở đây:

* một lượt gọi ghi **cả blob lẫn hai khóa settings** — không còn bước tay nào;
* gọi lại lần hai vẫn ăn (row_version đọc trong cùng transaction với lệnh ghi);
* kiểu tệp ngoài danh sách của `report.logo_media_type` bị chặn bằng lỗi
  nghiệp vụ, quyền ghi là `system.setting.edit`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.kernel.attachments.storage import blob_path
from ket.kernel.datasets.provisioning import DatasetRef
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"logo-5e" * 8


@pytest.fixture(scope="module")
def logo_storage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("logo-blobs")


@pytest.fixture
def client(
    test_settings: Settings,
    logo_storage: Path,
    app_engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    configured = test_settings.model_copy(update={"attachments_dir": logo_storage})
    with api_test_client(create_app(configured)) as instance:
        yield instance


@pytest.fixture(scope="module")
def editor_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        "sua_thiet_lap_logo",
        ["system.setting.view", "system.setting.edit"],
    )


@pytest.fixture(scope="module")
def viewer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory, dataset_alpha, "chi_xem_thiet_lap_logo", ["system.setting.view"]
    )


def _headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role: str,
    prefix: str,
    password: str,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset,
        user_factory,
        role,
        prefix,
        password,
        branch_codes=all_branch_codes(session_factory, dataset),
    )


def _setting_value(client: TestClient, headers: dict[str, str], key: str) -> str:
    listing = client.get("/api/v1/system/settings", headers=headers)
    assert listing.status_code == 200, listing.text
    item = next(entry for entry in listing.json()["items"] if entry["key"] == key)
    return str(item["value"])


def test_one_step_logo_upload_writes_blob_and_both_settings(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    test_password: str,
    logo_storage: Path,
) -> None:
    headers = _headers(
        client, session_factory, dataset_alpha, user_factory, editor_role, "logo-sua", test_password
    )
    response = client.post(
        "/api/v1/system/settings/logo",
        files={"file": ("logo.png", PNG_BYTES, "image/png")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["media_type"] == "image/png"
    assert body["byte_size"] == len(PNG_BYTES)

    content_hash = body["content_hash"]
    stored = blob_path(logo_storage, dataset_alpha.schema_name, content_hash)
    assert stored.read_bytes() == PNG_BYTES
    assert _setting_value(client, headers, "report.logo_content_hash") == content_hash
    assert _setting_value(client, headers, "report.logo_media_type") == "image/png"

    # Gọi lại lần hai (đổi logo): row_version đã tăng — nút một-bước vẫn phải
    # ăn mà không cần client cầm phiên bản nào.
    again = client.post(
        "/api/v1/system/settings/logo",
        files={"file": ("logo2.png", PNG_BYTES + b"v2", "image/png")},
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert (
        _setting_value(client, headers, "report.logo_content_hash")
        == (again.json()["content_hash"])
    )


def test_logo_upload_rejects_unsupported_media_type(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    test_password: str,
) -> None:
    headers = _headers(
        client, session_factory, dataset_alpha, user_factory, editor_role, "logo-txt", test_password
    )
    response = client.post(
        "/api/v1/system/settings/logo",
        files={"file": ("logo.txt", b"not an image", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "settings.value_invalid"


def test_logo_upload_requires_setting_edit_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    viewer_role: str,
    test_password: str,
) -> None:
    headers = _headers(
        client, session_factory, dataset_alpha, user_factory, viewer_role, "logo-xem", test_password
    )
    response = client.post(
        "/api/v1/system/settings/logo",
        files={"file": ("logo.png", PNG_BYTES, "image/png")},
        headers=headers,
    )
    assert response.status_code == 403, response.text
