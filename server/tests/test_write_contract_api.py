"""Hợp đồng ghi qua HTTP: idempotency, khóa lạc quan, tùy chọn hai cấp.

Test tầng dịch vụ đã kiểm từng cơ chế; ở đây kiểm **chuỗi** — header, mã trạng
thái, thân RFC 7807, luật phân quyền — tức là đúng những gì client thấy. Một cơ
chế đúng ở tầng dịch vụ nhưng không nối vào endpoint sẽ chỉ lộ ra ở đây.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.dependencies import DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.config.catalog import GRID_ENTER_KEY, LOCALE_KEY, MONEY_SCALE_KEY
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Permission, Role, RolePermission
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

WRITER_ROLE = "bien_tap_hop_dong_ghi"
"""Vai trò cho test này: xem/tạo/sửa chi nhánh + xem tùy chọn.

**Không** có `system.setting.edit` — nửa số test dưới đây kiểm đúng ranh giới
giữa "sửa tùy chọn của mình" và "sửa tùy chọn của cả doanh nghiệp"."""

ADMIN_SETTING_ROLE = "quan_tri_tuy_chon"

BRANCH_VIEW = permission_code(SYSTEM_MODULE, "branch", Action.VIEW)
BRANCH_CREATE = permission_code(SYSTEM_MODULE, "branch", Action.CREATE)
BRANCH_EDIT = permission_code(SYSTEM_MODULE, "branch", Action.EDIT)
SETTING_VIEW = permission_code(SYSTEM_MODULE, "setting", Action.VIEW)
SETTING_EDIT = permission_code(SYSTEM_MODULE, "setting", Action.EDIT)


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    app = create_app(test_settings)
    with api_test_client(app) as instance:
        yield instance


def _ensure_role(
    session_factory: sessionmaker[Session], dataset: DatasetRef, code: str, permissions: list[str]
) -> str:
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            role = Role(code=code, name=f"Vai trò {code}")
            session.add(role)
            session.flush()
            for permission in permissions:
                permission_id = session.scalar(
                    select(Permission.id).where(Permission.code == permission)
                )
                assert permission_id is not None, f"chưa gieo mã quyền {permission}"
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return code


@pytest.fixture(scope="module")
def writer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(
        session_factory,
        dataset_alpha,
        WRITER_ROLE,
        [BRANCH_VIEW, BRANCH_CREATE, BRANCH_EDIT, SETTING_VIEW],
    )


@pytest.fixture(scope="module")
def setting_admin_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(
        session_factory, dataset_alpha, ADMIN_SETTING_ROLE, [SETTING_VIEW, SETTING_EDIT]
    )


def _actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role_code: str,
    prefix: str,
) -> dict[str, str]:
    """Tạo người dùng có vai trò, đăng nhập, trả về headers dùng được ngay."""
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
    )
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}", DATASET_HEADER: dataset.code}


# --------------------------------------------------------------------------
# Idempotency qua HTTP (FR-NFR-004)
# --------------------------------------------------------------------------


def test_creating_a_branch_without_the_idempotency_header_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "thieukhoa")

    response = client.post(
        "/api/v1/system/branches", headers=headers, json={"code": "CN_THIEU", "name": "Thiếu khóa"}
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_resending_the_same_create_returns_the_first_branch_with_200(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Bấm Lưu, mất mạng, bấm lại — đúng một chi nhánh, và client biết điều đó."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "guilai")
    key = str(uuid4())
    payload = {"code": f"CN_{uuid4().hex[:8].upper()}", "name": "Chi nhánh gửi lại"}

    first = client.post(
        "/api/v1/system/branches", headers={**headers, IDEMPOTENCY_HEADER: key}, json=payload
    )
    second = client.post(
        "/api/v1/system/branches", headers={**headers, IDEMPOTENCY_HEADER: key}, json=payload
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, "lần gửi lại không tạo gì nên không phải 201"
    assert first.json()["id"] == second.json()["id"]

    listed = client.get("/api/v1/system/branches", headers=headers).json()["items"]
    assert len([item for item in listed if item["code"] == payload["code"]]) == 1


def test_the_same_key_with_a_different_body_is_a_conflict(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "khoakhac")
    key = str(uuid4())

    first = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: key},
        json={"code": f"CN_{uuid4().hex[:8].upper()}", "name": "Bản đầu"},
    )
    second = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: key},
        json={"code": f"CN_{uuid4().hex[:8].upper()}", "name": "Bản khác"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error_code"] == "idempotency.key_reused"


def test_an_oversized_key_is_refused_before_anything_is_written(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Khóa dài hơn cột phải hỏng ở cổng vào, không phải giữa lệnh `INSERT`."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "khoadai")
    code = f"CN_{uuid4().hex[:8].upper()}"

    response = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: "k" * 200},
        json={"code": code, "name": "Khóa quá dài"},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_invalid"

    listed = client.get("/api/v1/system/branches", headers=headers).json()["items"]
    assert not [item for item in listed if item["code"] == code]


# --------------------------------------------------------------------------
# Khóa lạc quan (FR-NFR-005)
# --------------------------------------------------------------------------


def test_the_second_client_to_save_gets_409_and_the_latest_record(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Hai người cùng mở một chi nhánh; người lưu sau không được ghi đè im lặng."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "hainguoi")
    created = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json={"code": f"CN_{uuid4().hex[:8].upper()}", "name": "Bản gốc"},
    ).json()

    stale_version = created["row_version"]

    first_save = client.put(
        f"/api/v1/system/branches/{created['id']}",
        headers=headers,
        json={
            "name": "Người thứ nhất sửa",
            "name_en": None,
            "is_active": True,
            "row_version": stale_version,
        },
    )
    assert first_save.status_code == 200, first_save.text
    assert first_save.json()["row_version"] == stale_version + 1

    second_save = client.put(
        f"/api/v1/system/branches/{created['id']}",
        headers=headers,
        json={
            "name": "Người thứ hai sửa",
            "name_en": None,
            "is_active": True,
            "row_version": stale_version,
        },
    )

    assert second_save.status_code == 409
    body = second_save.json()
    assert body["error_code"] == "concurrency.row_version_conflict"
    assert body["latest"]["name"] == "Người thứ nhất sửa", "phải trả bản mới nhất kèm theo"
    assert body["latest"]["row_version"] == stale_version + 1


def test_the_row_version_bump_is_not_noise_in_the_audit_log(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Nhật ký ghi thay đổi **nghiệp vụ**, không ghi bộ đếm phiên bản."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "vetsach")
    created = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json={"code": f"CN_{uuid4().hex[:8].upper()}", "name": "Trước khi sửa"},
    ).json()

    client.put(
        f"/api/v1/system/branches/{created['id']}",
        headers=headers,
        json={
            "name": "Sau khi sửa",
            "name_en": None,
            "is_active": True,
            "row_version": created["row_version"],
        },
    )

    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        from ket.kernel.auditing.models import AuditLog

        entries = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "branches", AuditLog.entity_id == str(created["id"]))
            .order_by(AuditLog.id)
        ).all()
        changes = [entry.new_values or {} for entry in entries]

    assert changes, "phải có vết cho cả lần tạo lẫn lần sửa"
    assert all("row_version" not in change for change in changes), (
        f"`row_version` lọt vào nhật ký: {changes}"
    )
    assert any(change.get("name") == "Sau khi sửa" for change in changes)


# --------------------------------------------------------------------------
# Tùy chọn hai cấp (FR-SYS-060, BR-SYS-06)
# --------------------------------------------------------------------------


def test_settings_start_from_the_catalog_default(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, writer_role, "mactuychon"
    )

    response = client.get("/api/v1/system/settings", headers=headers)

    assert response.status_code == 200, response.text
    values = {item["key"]: item for item in response.json()["items"]}
    assert values[MONEY_SCALE_KEY]["value"] == "2"
    assert values[MONEY_SCALE_KEY]["source"] == "default"
    assert values[MONEY_SCALE_KEY]["system_row_version"] is None
    assert values[MONEY_SCALE_KEY]["user_row_version"] is None
    assert values[LOCALE_KEY]["scopes"] == ["system", "user"]


def test_a_user_setting_overrides_the_system_one_for_that_user_only(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
    setting_admin_role: str,
) -> None:
    """Đây là toàn bộ ý nghĩa của "hai cấp": chung là mặc định, riêng là quyết định."""
    admin = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "quantrituychon"
    )
    member = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "thanhvien")

    system_write = client.put(
        f"/api/v1/system/settings/{GRID_ENTER_KEY}",
        headers=admin,
        json={"scope": "system", "value": "false", "row_version": None},
    )
    assert system_write.status_code == 200, system_write.text
    assert system_write.json()["source"] == "system"

    member_before = {
        item["key"]: item
        for item in client.get("/api/v1/system/settings", headers=member).json()["items"]
    }
    assert member_before[GRID_ENTER_KEY]["value"] == "false"

    member_write = client.put(
        f"/api/v1/system/settings/{GRID_ENTER_KEY}",
        headers=member,
        json={"scope": "user", "value": "true", "row_version": None},
    )
    assert member_write.status_code == 200, member_write.text
    assert member_write.json()["value"] == "true"
    assert member_write.json()["source"] == "user"

    admin_after = {
        item["key"]: item
        for item in client.get("/api/v1/system/settings", headers=admin).json()["items"]
    }
    assert admin_after[GRID_ENTER_KEY]["value"] == "false", (
        "tùy chọn riêng không được rò sang người khác"
    )


def test_writing_a_system_setting_needs_its_own_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Xem được tùy chọn ≠ đổi được cách ghi sổ của cả doanh nghiệp."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, writer_role, "khongduocsua"
    )

    response = client.put(
        f"/api/v1/system/settings/{MONEY_SCALE_KEY}",
        headers=headers,
        json={"scope": "system", "value": "0", "row_version": None},
    )

    assert response.status_code == 403
    assert response.json()["details"]["permission"] == SETTING_EDIT


def test_a_setting_that_is_system_only_cannot_be_set_per_user(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Hai người nhập cùng một hóa đơn phải ra cùng một con số (FR-NFR-002)."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, writer_role, "scaleriêng"
    )

    response = client.put(
        f"/api/v1/system/settings/{MONEY_SCALE_KEY}",
        headers=headers,
        json={"scope": "user", "value": "4", "row_version": None},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "settings.scope_not_allowed"


def test_a_value_outside_the_declared_bounds_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    setting_admin_role: str,
) -> None:
    """`money.scale = 9` phải hỏng ở màn hình thiết lập, không phải giữa kỳ tính giá."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "ngoaikhoang"
    )

    response = client.put(
        f"/api/v1/system/settings/{MONEY_SCALE_KEY}",
        headers=headers,
        json={"scope": "system", "value": "9", "row_version": None},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "settings.value_invalid"


def test_an_unknown_setting_key_is_a_404(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    setting_admin_role: str,
) -> None:
    """Catalog đóng: bảng tùy chọn không phải chỗ chứa mọi thứ chưa kịp thiết kế."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "khoala"
    )

    response = client.put(
        "/api/v1/system/settings/khoa.khong.co.that",
        headers=headers,
        json={"scope": "system", "value": "1", "row_version": None},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "settings.key_unknown"


def test_saving_a_setting_on_a_stale_version_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    setting_admin_role: str,
) -> None:
    """Khóa lạc quan áp cho cả tùy chọn, không chỉ cho danh mục."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "tuychoncu"
    )

    first = client.put(
        f"/api/v1/system/settings/{LOCALE_KEY}",
        headers=headers,
        json={"scope": "system", "value": "en", "row_version": None},
    )
    assert first.status_code == 200, first.text
    version = first.json()["system_row_version"]

    stale = client.put(
        f"/api/v1/system/settings/{LOCALE_KEY}",
        headers=headers,
        json={"scope": "system", "value": "vi", "row_version": None},
    )

    assert stale.status_code == 409, "gửi `null` khi dòng đã tồn tại cũng là một khẳng định sai"
    assert stale.json()["error_code"] == "concurrency.row_version_conflict"

    fresh = client.put(
        f"/api/v1/system/settings/{LOCALE_KEY}",
        headers=headers,
        json={"scope": "system", "value": "vi", "row_version": version},
    )
    assert fresh.status_code == 200
    assert fresh.json()["value"] == "vi"


def _settings_of(client: TestClient, headers: dict[str, str]) -> dict[str, dict[str, object]]:
    """Bảng tùy chọn đang hiệu lực cho người gọi, tra theo khóa.

    Các test trong module dùng chung một dữ liệu kế toán, nên một khóa có thể đã
    được test khác ghi trước đó. Đọc hiện trạng rồi mới ghi là cách viết đúng ở
    đây — và cũng là cách client thật hành xử.
    """
    response = client.get("/api/v1/system/settings", headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"]: item for item in response.json()["items"]}


def test_a_user_can_override_a_system_value_that_already_exists(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
    setting_admin_role: str,
) -> None:
    """Đặt giá trị riêng khi **đã có** giá trị chung — tính năng của FR-SYS-060.

    Hợp đồng cũ trả đúng một `row_version` (của giá trị đang hiệu lực) và bảo
    client gửi lại nó; lúc ghi thì so với dòng của cấp được yêu cầu. Người dùng
    làm đúng theo hợp đồng vẫn nhận `409` — tức là không ai đặt được tùy chọn
    riêng một khi quản trị viên đã đặt giá trị chung.
    """
    admin = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "adminlocale"
    )
    member = _actor(
        client, session_factory, dataset_alpha, user_factory, writer_role, "riengcuatoi"
    )

    system_write = client.put(
        f"/api/v1/system/settings/{LOCALE_KEY}",
        headers=admin,
        json={
            "scope": "system",
            "value": "en",
            "row_version": _settings_of(client, admin)[LOCALE_KEY]["system_row_version"],
        },
    )
    assert system_write.status_code == 200, system_write.text

    seen = _settings_of(client, member)[LOCALE_KEY]
    assert seen["source"] == "system"
    assert seen["system_row_version"] is not None
    assert seen["user_row_version"] is None, "chưa có dòng riêng nào"

    # Client gửi đúng phiên bản của **cấp mình đang sửa**.
    override = client.put(
        f"/api/v1/system/settings/{LOCALE_KEY}",
        headers=member,
        json={"scope": "user", "value": "vi", "row_version": seen["user_row_version"]},
    )

    assert override.status_code == 200, override.text
    assert override.json()["value"] == "vi"
    assert override.json()["source"] == "user"


def test_a_user_value_does_not_license_overwriting_the_system_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    setting_admin_role: str,
) -> None:
    """Ghi đè im lặng — đúng thứ FR-NFR-005 sinh ra để chặn.

    Quản trị viên A có giá trị riêng ở phiên bản N; người khác đã sửa giá trị
    chung tới cũng phiên bản N. Màn hình của A chỉ hiện giá trị **của A**, nên A
    chưa từng thấy dòng chung mới. Với hợp đồng cũ (một `row_version` duy nhất),
    A lưu ở cấp hệ thống và ghi đè dòng đó, không cảnh báo gì.
    """
    admin = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "ghideim"
    )
    other = _actor(
        client, session_factory, dataset_alpha, user_factory, setting_admin_role, "nguoikhac"
    )

    # A đặt giá trị riêng → dòng user của A ở v1.
    mine = client.put(
        f"/api/v1/system/settings/{GRID_ENTER_KEY}",
        headers=admin,
        json={"scope": "user", "value": "false", "row_version": None},
    )
    assert mine.status_code == 200, mine.text
    my_version = mine.json()["user_row_version"]

    # Người khác đặt giá trị chung. A không thấy gì — màn hình của A hiện giá
    # trị riêng của A.
    theirs = client.put(
        f"/api/v1/system/settings/{GRID_ENTER_KEY}",
        headers=other,
        json={
            "scope": "system",
            "value": "true",
            "row_version": _settings_of(client, other)[GRID_ENTER_KEY]["system_row_version"],
        },
    )
    assert theirs.status_code == 200, theirs.text

    # Điều kiện của phép thử: hai phiên bản phải khác nhau, nếu không test này
    # sẽ xanh vì trùng hợp chứ không vì mã đúng.
    system_version = _settings_of(client, other)[GRID_ENTER_KEY]["system_row_version"]
    assert my_version != system_version, "hai cấp đang cùng phiên bản — phép thử vô nghĩa"

    # A gửi phiên bản của dòng **riêng** nhưng lưu ở cấp **hệ thống** — đúng
    # điều hợp đồng cũ mời gọi, vì nó chỉ trả về một `row_version` duy nhất.
    stale = client.put(
        f"/api/v1/system/settings/{GRID_ENTER_KEY}",
        headers=admin,
        json={"scope": "system", "value": "false", "row_version": my_version},
    )

    assert stale.status_code == 409, "ghi đè được một dòng chưa từng nhìn thấy"
    assert stale.json()["error_code"] == "concurrency.row_version_conflict"
    assert stale.json()["latest"]["scope"] == "system"

    effective = {
        item["key"]: item
        for item in client.get("/api/v1/system/settings", headers=other).json()["items"]
    }[GRID_ENTER_KEY]
    assert effective["value"] == "true", "giá trị chung của người kia đã bị ghi đè"


def test_a_duplicate_branch_code_is_a_409_not_a_500(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    writer_role: str,
) -> None:
    """Lỗi gõ tay thường gặp nhất của người nhập liệu.

    Trước khi có handler `IntegrityError`, gõ trùng mã chi nhánh trả về "Đã xảy
    ra lỗi không mong muốn — cung cấp mã tham chiếu cho bộ phận hỗ trợ". Người
    nhập liệu không làm được gì với câu đó.

    Dùng **khóa idempotency mới** cho lần thứ hai: cùng khóa thì đã là đường
    gửi-lại (trả `200`), còn đây là hai thao tác khác nhau tình cờ trùng mã.
    """
    headers = _actor(client, session_factory, dataset_alpha, user_factory, writer_role, "trungma")
    code = f"CN_{uuid4().hex[:8].upper()}"

    first = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json={"code": code, "name": "Bản đầu"},
    )
    second = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json={"code": code, "name": "Trùng mã"},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["error_code"] == "data.duplicate"
    assert body["details"]["constraint"] == "uq_branches_code"
    # Không lộ câu lệnh, tên cột hay giá trị dữ liệu.
    assert "INSERT" not in body["detail"] and code not in body["detail"]
