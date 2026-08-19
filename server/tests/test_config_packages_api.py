"""Tầng HTTP của `/api/v1/config-packages` (lát 5A).

Bốn việc kiểm ở đây: cổng quyền (401/403), kích hoạt idempotent (đúng khuôn
`post_voucher`), nhập gói `.zip` qua HTTP thành công lẫn bị từ chối. Quyền
`system.config_package.*` đòi 2FA (cùng mức `system.installation`) nên actor
"có quyền" phải đi trọn vòng đăng ký thiết bị trước khi gọi được endpoint —
xem `_full_actor`.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.api.routers.config_packages import (
    CONFIG_PACKAGE_ACTIVATE,
    CONFIG_PACKAGE_IMPORT,
    CONFIG_PACKAGE_VIEW,
)
from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service, totp
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def full_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        "quan_tri_goi_cau_hinh",
        [CONFIG_PACKAGE_VIEW, CONFIG_PACKAGE_ACTIVATE, CONFIG_PACKAGE_IMPORT],
    )


@pytest.fixture(scope="module")
def outsider_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, "vai_tro_ngoai_cuoc_5a", [])


def _login(client: TestClient, username: str, password: str, **extra: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password, **extra}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _auth(token: str, dataset: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", DATASET_HEADER: dataset}


def _full_actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role_code: str,
    prefix: str,
    password: str,
) -> dict[str, str]:
    """Người dùng đã qua đăng ký 2FA — bắt buộc cho `system.config_package.*`."""
    user = user_factory(prefix, password=password)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    for branch_code in all_branch_codes(session_factory, dataset):
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset.schema_name,
            user_id=user.id,
            branch_code=branch_code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )

    limited = _login(client, user.username, password)
    assert limited["session_scope"] == "totp_enrollment"
    headers = _auth(str(limited["token"]), dataset.code)

    enrolled = client.post("/api/v1/auth/totp/enroll", headers=headers, json={"password": password})
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["provisioning_uri"].split("secret=")[1].split("&")[0]
    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    confirmed = client.post(
        "/api/v1/auth/totp/confirm", headers=headers, json={"code": generator.now()}
    )
    assert confirmed.status_code == 204, confirmed.text

    next_code = generator.at(datetime.now(UTC) + timedelta(seconds=totp.PERIOD_SECONDS))
    full = _login(client, user.username, password, totp_code=next_code)
    assert full["session_scope"] == "full"
    return _auth(str(full["token"]), dataset.code)


@pytest.fixture
def full_actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    full_role: str,
    test_password: str,
) -> dict[str, str]:
    """Đăng nhập lại + qua trọn vòng 2FA cho **mỗi** test — `client` là fixture
    theo hàm (mỗi test một `TestClient`), nên fixture phụ thuộc nó không được
    rộng phạm vi hơn `function` (pytest chặn scope mismatch)."""
    return _full_actor(
        client, session_factory, dataset_alpha, user_factory, full_role, "cfgpkg5a", test_password
    )


@pytest.fixture
def outsider(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    outsider_role: str,
    test_password: str,
) -> dict[str, str]:
    user = user_factory("cfgpkg5a_out", password=test_password)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=outsider_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    token = _login(client, user.username, test_password)["token"]
    return _auth(str(token), dataset_alpha.code)


# ---------------------------------------------------------------------------
# Cổng quyền
# ---------------------------------------------------------------------------


def test_list_requires_authentication(client: TestClient) -> None:
    response = client.get("/api/v1/config-packages")
    assert response.status_code == 401


def test_list_requires_permission(client: TestClient, outsider: dict[str, str]) -> None:
    response = client.get("/api/v1/config-packages", headers=outsider)
    assert response.status_code == 403


def test_list_returns_builtin_packages(client: TestClient, full_actor: dict[str, str]) -> None:
    response = client.get("/api/v1/config-packages", headers=full_actor)
    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["items"]}
    assert {"TT99-2025", "TT133-2016"} <= codes


def test_get_accounts_of_unknown_package_is_404(
    client: TestClient, full_actor: dict[str, str]
) -> None:
    response = client.get("/api/v1/config-packages/999999999/accounts", headers=full_actor)
    assert response.status_code == 404


def test_get_accounts_returns_the_tree(
    client: TestClient,
    full_actor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        package_id = session.scalar(
            select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025")
        )
    response = client.get(f"/api/v1/config-packages/{package_id}/accounts", headers=full_actor)
    assert response.status_code == 200
    body = response.json()
    assert body["package_id"] == package_id
    assert any(item["code"] == "111" for item in body["items"])


# ---------------------------------------------------------------------------
# Kích hoạt — idempotent
# ---------------------------------------------------------------------------


def test_activate_requires_permission(client: TestClient, outsider: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/config-packages/1/actions/activate",
        headers={**outsider, IDEMPOTENCY_HEADER: "khong-dung-toi"},
    )
    assert response.status_code == 403


def test_activate_requires_idempotency_key(client: TestClient, full_actor: dict[str, str]) -> None:
    response = client.post("/api/v1/config-packages/1/actions/activate", headers=full_actor)
    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_activate_is_idempotent_on_replay(
    client: TestClient,
    full_actor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    # Gói THÊM RIÊNG cho test này, không đụng `TT99-2025` builtin.
    # `dataset_alpha` dùng chung với hàng trăm test khác trong cùng phiên, và
    # `resolve_package` (sửa sau review, C1) xếp gói theo `effective_from DESC,
    # activated_at DESC` — kích hoạt LẠI builtin ở đây sẽ đẩy `activated_at`
    # của nó lên "vừa xong", khiến nó thắng gói tối giản của
    # `posting_support.seed_posting_context` (cùng `effective_from`) cho MỌI
    # test chạy sau — một lượt test tưởng vô hại làm sai lệch hệ TK mà hàng
    # chục test khác đang dựa vào (đã đo được: ~40 test posting đỏ theo).
    # Gói riêng, `scheme=TT99` giống hệt (cùng scheme thì kích hoạt luôn được
    # phép — không đụng cổng FR-SYS-004; cổng đó có test riêng ở
    # `test_config_package_activator.py`, dataset riêng), nhưng KHÔNG được bất
    # kỳ chứng từ nào resolve tới trước khi test này chạy — cô lập hoàn toàn.
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        throwaway = ConfigPackage(
            code="ZZ-API-IDEMPOTENT-ACTIVATE",
            name="Gói riêng cho test idempotent",
            scheme="TT99",
            effective_from=date(2040, 1, 1),
            is_builtin=False,
            activated_at=None,
            activated_by=None,
        )
        session.add(throwaway)
        session.flush()
        package_id = throwaway.id

    key = "activate-key-tt99-once"
    first = client.post(
        f"/api/v1/config-packages/{package_id}/actions/activate",
        headers={**full_actor, IDEMPOTENCY_HEADER: key},
    )
    assert first.status_code == 200, first.text
    activated_at_first = first.json()["activated_at"]
    assert activated_at_first is not None

    second = client.post(
        f"/api/v1/config-packages/{package_id}/actions/activate",
        headers={**full_actor, IDEMPOTENCY_HEADER: key},
    )
    assert second.status_code == 200
    # So bằng **giá trị thời điểm**, không so chuỗi: `TIMESTAMPTZ` đọc lại qua
    # psycopg mang offset theo timezone của phiên (+07:00), còn lần ghi đầu là
    # `datetime.now(UTC)` — cùng một thời điểm, hai cách biểu diễn chuỗi khác
    # nhau (`Z` so với `+07:00`). Gửi lại cùng khóa không được kích hoạt lại,
    # tức phải ra đúng **thời điểm** của lần đầu.
    assert datetime.fromisoformat(second.json()["activated_at"]) == datetime.fromisoformat(
        activated_at_first
    ), "gửi lại cùng khóa phải trả về đúng kết quả lần đầu, không kích hoạt lại"

    # Dọn ngay: gói này kích hoạt xong có `effective_from=2040-01-01`, hiệu lực
    # mở (`effective_to=NULL`) — để lại nó sẽ thắng MỌI gói TT99 khác
    # (`effective_from` muộn hơn cả builtin lẫn gói test của
    # `posting_support`) cho bất kỳ ngày hạch toán nào từ 2040 trở đi, làm sai
    # lệch `resolve_package` của những test chạy sau dùng năm tài chính xa
    # (đã đo được: `test_period_lock_service.py` tạo năm 2073 để tránh đụng
    # năm 2026 dùng chung, và "tránh đụng" đó thất bại nếu gói này còn sống).
    with unit_of_work(session_factory, scope) as session:
        leftover = session.get(ConfigPackage, package_id)
        if leftover is not None:
            session.delete(leftover)


# ---------------------------------------------------------------------------
# Nhập gói .zip
# ---------------------------------------------------------------------------


def _sample_zip(signer: Ed25519PrivateKey, *, code: str) -> bytes:
    manifest_file = "package.json"
    accounts_file = "accounts.csv"
    default_accounts_file = "default_accounts.csv"
    closing_pairs_file = "closing_pairs.csv"

    files = {
        manifest_file: (
            f'{{"code": "{code}", "scheme": "TT99", "name": "Gói API test", '
            '"name_en": null, "description": null, "legal_reference": null, '
            '"effective_from": "2020-01-01", "effective_to": null, "version": 1}'
        ).encode(),
        accounts_file: (
            "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
            "detail_tracking,is_locked\n111,Tiền mặt,,,0,0,0,,1\n"
        ).encode(),
        default_accounts_file: b"document_type,purpose,account_code\n*,cash,111\n",
        closing_pairs_file: (b"source_account,target_account,sequence,description\n"),
    }
    manifest = {
        "package": {"code": code, "version": 1},
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    signature = signer.sign(manifest_bytes)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("manifest.sig", signature)
    return buffer.getvalue()


def test_import_requires_permission(client: TestClient, outsider: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/config-packages/import",
        headers={**outsider, IDEMPOTENCY_HEADER: "khong-dung-toi"},
        files={"file": ("goi.zip", b"noi dung khong quan trong", "application/zip")},
    )
    assert response.status_code == 403


def test_import_rejects_a_zip_not_signed_by_the_pinned_key(
    client: TestClient, full_actor: dict[str, str]
) -> None:
    """Không monkeypatch khóa ghim — gói ký bằng khóa lạ phải bị từ chối bởi
    đúng đường sản xuất (`publisher_keys.pinned_public_keys()`)."""
    rogue = Ed25519PrivateKey.generate()
    archive_bytes = _sample_zip(rogue, code="API-IMPORT-REJECT")

    response = client.post(
        "/api/v1/config-packages/import",
        headers={**full_actor, IDEMPOTENCY_HEADER: "import-reject-key"},
        files={"file": ("goi.zip", archive_bytes, "application/zip")},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "config.package_signature_invalid"


def test_import_accepts_a_zip_signed_by_the_pinned_key(
    client: TestClient, full_actor: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tiêm một khóa dùng-một-lần làm "khóa ghim" cho đúng một test — không đụng
    `publisher_keys.py` thật, chỉ patch điểm `importer.py` gọi tới nó.
    """
    signer = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        "ket.kernel.config.packages.importer.pinned_public_keys",
        lambda: (signer.public_key(),),
    )
    archive_bytes = _sample_zip(signer, code="API-IMPORT-ACCEPT")

    response = client.post(
        "/api/v1/config-packages/import",
        headers={**full_actor, IDEMPOTENCY_HEADER: "import-accept-key"},
        files={"file": ("goi.zip", archive_bytes, "application/zip")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["code"] == "API-IMPORT-ACCEPT"
    assert response.json()["is_builtin"] is False
