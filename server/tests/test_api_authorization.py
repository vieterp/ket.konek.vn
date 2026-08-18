"""Chuỗi định tuyến + phân quyền đầu-cuối qua HTTP (bước 8 của phase 2).

Test tầng dịch vụ đã kiểm từng mảnh; ở đây kiểm **chuỗi**: header `X-Dataset` →
`SET ROLE` vai trò dataset → `search_path` → GUC chi nhánh → kiểm quyền →
transaction nghiệp vụ. Một mắt xích quên nối sẽ không lộ ra ở tầng dịch vụ, vì
ở đó test tự dựng `RequestScope` bằng tay.

Ba bất biến đắt nhất nằm ở đây:

* cô lập chi nhánh do **RLS** làm, không do bộ lọc trong câu truy vấn — chứng
  minh bằng `/system/audit-log`, endpoint cố ý không có `WHERE branch_id`;
* phiên hạn chế chỉ mở đúng đường đăng ký 2FA, và vòng đăng ký đó đi trọn được
  bằng HTTP (không cần ai chạm máy chủ);
* mật khẩu tạm chặn được ở **server**, không chỉ là một cờ gợi ý cho client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER, DATASET_HEADER
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service, totp
from ket.kernel.security.models import Branch, Permission, Role, RolePermission
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

BRANCH_VIEW = permission_code(SYSTEM_MODULE, "branch", Action.VIEW)
BRANCH_CREATE = permission_code(SYSTEM_MODULE, "branch", Action.CREATE)
AUDIT_VIEW = permission_code(SYSTEM_MODULE, "audit_log", Action.VIEW)
USER_EDIT = permission_code(SYSTEM_MODULE, "user", Action.EDIT)

STAFF_ROLE = "nhan_vien_api"
"""Vai trò **không** mang quyền nhạy cảm — nên không kéo theo 2FA.

Phần lớn test dưới đây không quan tâm tới 2FA, và dùng `admin` sẽ khiến mọi lần
đăng nhập trả về phiên hạn chế. Đó là hành vi đúng, nhưng nó sẽ che mất thứ đang
được kiểm."""


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """App thật chạy qua `lifespan` thật, trên database của bộ fixture."""
    assert app_engine is not None and session_factory is not None
    app = create_app(test_settings)
    with api_test_client(app) as instance:
        yield instance


@pytest.fixture(scope="module")
def staff_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Vai trò chỉ có ba quyền đọc/tạo, không chạm `system.user`/`system.role`."""
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == STAFF_ROLE)) is None:
            role = Role(code=STAFF_ROLE, name="Nhân viên (test)")
            session.add(role)
            session.flush()
            for code in (BRANCH_VIEW, BRANCH_CREATE, AUDIT_VIEW):
                permission_id = session.scalar(select(Permission.id).where(Permission.code == code))
                assert permission_id is not None
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return STAFF_ROLE


@pytest.fixture(scope="module")
def api_branches(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> list[str]:
    """Hai chi nhánh để kiểm cô lập; tạo một lần cho cả module."""
    codes = ["CN_API_A", "CN_API_B"]
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        existing = set(session.scalars(select(Branch.code).where(Branch.code.in_(codes))).all())
        for code in codes:
            if code not in existing:
                BranchService(session).create(code=code, name=f"Chi nhánh {code}")
    return codes


def _login(client: TestClient, username: str, **extra: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD, **extra}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _auth(token: str, dataset: str | None = None, branch: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if dataset is not None:
        headers[DATASET_HEADER] = dataset
    if branch is not None:
        headers[BRANCH_HEADER] = str(branch)
    return headers


def _staff(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
    prefix: str,
    *,
    branch_code: str | None = None,
) -> User:
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=staff_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    if branch_code is not None:
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset.schema_name,
            user_id=user.id,
            branch_code=branch_code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )
    return user


# --------------------------------------------------------------------------
# Định tuyến dataset
# --------------------------------------------------------------------------


def test_a_business_endpoint_without_the_dataset_header_is_refused(
    client: TestClient, user_factory: UserFactory
) -> None:
    """Không có mặc định "dataset đầu tiên" — đoán sai là ghi sổ nhầm doanh nghiệp."""
    token = _login(client, user_factory("thieuheader").username)["token"]
    response = client.get("/api/v1/system/branches", headers=_auth(token))

    assert response.status_code == 400
    assert response.json()["error_code"] == "dataset.header_missing"


def test_an_unknown_dataset_code_is_not_routed_anywhere(
    client: TestClient, user_factory: UserFactory
) -> None:
    """Mã bịa không được trở thành `search_path` trỏ vào schema không tồn tại."""
    token = _login(client, user_factory("datasetbia").username)["token"]
    response = client.get("/api/v1/system/branches", headers=_auth(token, "khong_co_that"))

    assert response.status_code == 422
    assert response.json()["error_code"] == "dataset.not_found"


def test_a_user_without_a_role_in_the_dataset_is_told_so(
    client: TestClient, user_factory: UserFactory, dataset_alpha: DatasetRef
) -> None:
    token = _login(client, user_factory("khongphan").username)["token"]
    response = client.get("/api/v1/system/branches", headers=_auth(token, dataset_alpha.code))

    assert response.status_code == 403
    assert response.json()["error_code"] == "dataset.access_denied"


def test_the_dataset_list_needs_no_dataset_header(
    client: TestClient, user_factory: UserFactory, dataset_alpha: DatasetRef
) -> None:
    """Đây chính là endpoint client gọi để biết đặt gì vào header đó."""
    token = _login(client, user_factory("danhsachds").username)["token"]
    response = client.get("/api/v1/system/datasets", headers=_auth(token))

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["items"]}
    assert dataset_alpha.code in codes


# --------------------------------------------------------------------------
# Kiểm quyền
# --------------------------------------------------------------------------


def test_a_permitted_action_succeeds_and_an_unpermitted_one_names_the_missing_code(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
) -> None:
    user = _staff(session_factory, dataset_alpha, user_factory, staff_role, "cophep")
    token = str(_login(client, user.username)["token"])
    headers = _auth(token, dataset_alpha.code)

    assert client.get("/api/v1/system/branches", headers=headers).status_code == 200

    # Vai trò này không có `system.user.edit` → đường gán chi nhánh phải đóng.
    forbidden = client.post(
        f"/api/v1/system/users/{user.id}/branches",
        headers=headers,
        json={"branch_code": "CN_API_A"},
    )
    assert forbidden.status_code == 403
    body = forbidden.json()
    assert body["error_code"] == "auth.permission_denied"
    assert body["details"]["permission"] == USER_EDIT


def test_access_endpoint_reports_the_effective_permissions(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
    api_branches: list[str],
) -> None:
    user = _staff(
        session_factory,
        dataset_alpha,
        user_factory,
        staff_role,
        "quyencuatoi",
        branch_code=api_branches[0],
    )
    token = str(_login(client, user.username)["token"])
    response = client.get("/api/v1/system/access", headers=_auth(token, dataset_alpha.code))

    assert response.status_code == 200
    body = response.json()
    assert body["dataset_code"] == dataset_alpha.code
    assert BRANCH_VIEW in body["permissions"]
    assert USER_EDIT not in body["permissions"]
    # Đúng một chi nhánh → suy ra chi nhánh đang thao tác, không bắt client gửi header.
    assert body["acting_branch_id"] == body["branch_ids"][0]


def test_an_acting_branch_outside_the_scope_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
    api_branches: list[str],
) -> None:
    user = _staff(
        session_factory,
        dataset_alpha,
        user_factory,
        staff_role,
        "chinhanhngoai",
        branch_code=api_branches[0],
    )
    token = str(_login(client, user.username)["token"])
    mine = client.get("/api/v1/system/access", headers=_auth(token, dataset_alpha.code)).json()

    response = client.get(
        "/api/v1/system/branches",
        headers=_auth(token, dataset_alpha.code, branch=mine["branch_ids"][0] + 10_000),
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"


# --------------------------------------------------------------------------
# Cô lập chi nhánh — do RLS làm, không do câu truy vấn
# --------------------------------------------------------------------------


def test_the_audit_log_shows_only_the_callers_branches(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
    api_branches: list[str],
) -> None:
    """`/system/audit-log` cố ý **không** lọc chi nhánh trong câu truy vấn.

    Nếu cô lập nằm ở tầng ứng dụng thì test này xanh chỉ khi ai đó nhớ viết
    `WHERE branch_id IN (...)`. Nó xanh vì RLS trên bảng gốc — và đó là điều
    khiến `SUM() OVER ()` hay bất kỳ báo cáo tổng hợp nào của phase sau cũng
    không rò được.
    """
    a_user = _staff(
        session_factory,
        dataset_alpha,
        user_factory,
        staff_role,
        "cn_a",
        branch_code=api_branches[0],
    )
    b_user = _staff(
        session_factory,
        dataset_alpha,
        user_factory,
        staff_role,
        "cn_b",
        branch_code=api_branches[1],
    )

    a_headers = _auth(str(_login(client, a_user.username)["token"]), dataset_alpha.code)
    b_headers = _auth(str(_login(client, b_user.username)["token"]), dataset_alpha.code)

    a_branch = client.get("/api/v1/system/access", headers=a_headers).json()["branch_ids"][0]
    b_branch = client.get("/api/v1/system/access", headers=b_headers).json()["branch_ids"][0]
    assert a_branch != b_branch

    seen = client.get("/api/v1/system/audit-log?page_size=200", headers=a_headers)
    assert seen.status_code == 200
    branches = {entry["branch_id"] for entry in seen.json()["items"]}

    # Dòng nhật ký của lần gán chi nhánh A mang đúng chi nhánh A và phải thấy;
    # dòng của chi nhánh B thì không, dù cùng bảng, cùng dataset.
    assert a_branch in branches
    assert b_branch not in branches


# --------------------------------------------------------------------------
# Trạng thái phiên: mật khẩu tạm và 2FA
# --------------------------------------------------------------------------


def test_a_temporary_password_blocks_everything_except_changing_it(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    staff_role: str,
) -> None:
    """Ép ở server, không chỉ báo cho client (FR-SYS-075)."""
    user = _staff(session_factory, dataset_alpha, user_factory, staff_role, "matkhautam")
    with_temp = user_factory("matkhautam_2", must_change_password=True)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=with_temp.id,
        role_code=staff_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )

    body = _login(client, with_temp.username)
    assert body["must_change_password"] is True
    headers = _auth(str(body["token"]), dataset_alpha.code)

    blocked = client.get("/api/v1/system/branches", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error_code"] == "auth.password_change_required"

    # `/auth/me` vẫn phải trả lời — nếu không client không biết vì sao bị chặn.
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    changed = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "Moi#MatKhau2026"},
    )
    assert changed.status_code == 204


def test_a_wrong_current_password_counts_towards_the_lockout(
    client: TestClient, user_factory: UserFactory
) -> None:
    """Cửa sau đổi mật khẩu không được dò được vô hạn và không dấu vết.

    Ai chiếm được một token phiên bỏ quên trên máy trạm sẽ thử ở đây, chứ không
    thử ở màn hình đăng nhập — nơi được canh rất kỹ.
    """
    user = user_factory("docuasau")
    headers = _auth(str(_login(client, user.username)["token"]))

    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "Sai#MatKhau2026", "new_password": "Moi#MatKhau2026"},
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "auth.invalid_credentials"

    with_session = client.app.state.session_factory  # type: ignore[attr-defined]
    from ket.kernel.persistence.session import control_session

    with control_session(with_session) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        assert reloaded.failed_login_count == 1


def test_a_user_forced_into_two_factor_can_enrol_without_touching_the_server(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
) -> None:
    """Vòng đời đầy đủ của điểm chặn H4, đi trọn bằng HTTP.

    Gán vai trò `admin` bật `totp_required`. Trước lát này, đó là một tài khoản
    **không đăng nhập lại được** cho tới khi ai đó chạy lệnh tại máy chủ. Ở đây
    nó tự thoát ra: phiên hạn chế → đăng ký thiết bị → xác nhận → đăng nhập lại
    với mã → phiên đầy đủ.
    """
    user = user_factory("tuthoat")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code="admin",
        actor_user_id=user.id,
        actor_permissions=None,
    )

    limited = _login(client, user.username)
    assert limited["session_scope"] == "totp_enrollment"
    headers = _auth(str(limited["token"]), dataset_alpha.code)

    blocked = client.get("/api/v1/system/branches", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error_code"] == "auth.session_scope_limited"

    enrolled = client.post("/api/v1/auth/totp/enroll", headers=headers, json={"password": PASSWORD})
    assert enrolled.status_code == 200
    secret = enrolled.json()["provisioning_uri"].split("secret=")[1].split("&")[0]

    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    confirmed = client.post(
        "/api/v1/auth/totp/confirm", headers=headers, json={"code": generator.now()}
    )
    assert confirmed.status_code == 204

    # Phiên hạn chế **không** tự nâng cấp: nó được cấp cho một tài khoản chưa
    # qua lớp thứ hai, nên nó phải chết ngay khi việc của nó xong.
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

    # Mã kế tiếp, không phải mã vừa dùng để xác nhận: chống phát lại
    # (`totp_last_counter`) từ chối đúng mã đó, và nó từ chối đúng.
    next_code = generator.at(datetime.now(UTC) + timedelta(seconds=totp.PERIOD_SECONDS))
    full = _login(client, user.username, totp_code=next_code)
    assert full["session_scope"] == "full"
    granted = client.get(
        "/api/v1/system/branches", headers=_auth(str(full["token"]), dataset_alpha.code)
    )
    assert granted.status_code == 200


def test_login_without_the_code_still_asks_for_it_once_enrolled(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
) -> None:
    """Sau khi đăng ký, thiếu mã là `auth.totp_required` — không phải phiên hạn chế."""
    user = user_factory("dadangky")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code="admin",
        actor_user_id=user.id,
        actor_permissions=None,
    )
    limited = _login(client, user.username)
    headers = _auth(str(limited["token"]))
    enrolled = client.post("/api/v1/auth/totp/enroll", headers=headers, json={"password": PASSWORD})
    secret = enrolled.json()["provisioning_uri"].split("secret=")[1].split("&")[0]
    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    client.post("/api/v1/auth/totp/confirm", headers=headers, json={"code": generator.now()})

    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "auth.totp_required"


# --------------------------------------------------------------------------
# Không tự nới được phạm vi của chính mình
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def user_admin_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Vai trò có `system.user.edit` — quyền gán chi nhánh cho người khác.

    Tách khỏi `admin` để test không phải đi trọn vòng 2FA: `system.user.edit`
    thuộc diện bắt buộc 2FA nên vai trò này vẫn bật cờ, nhưng ở đây ta gán nó
    qua tầng dịch vụ và đăng nhập bằng phiên hạn chế thì không đủ — nên các test
    dùng nó phải enrol thật. Xem `_full_session`.
    """
    code = "quan_tri_nguoi_dung_api"
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            role = Role(code=code, name="Quản trị người dùng (test)")
            session.add(role)
            session.flush()
            for permission in (USER_EDIT, BRANCH_VIEW):
                permission_id = session.scalar(
                    select(Permission.id).where(Permission.code == permission)
                )
                assert permission_id is not None
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return code


def _full_session(client: TestClient, username: str) -> str:
    """Đăng nhập tới **phiên đầy đủ**, đi trọn vòng đăng ký 2FA nếu bị đòi."""
    body = _login(client, username)
    if body["session_scope"] == "full":
        return str(body["token"])

    headers = _auth(str(body["token"]))
    enrolled = client.post("/api/v1/auth/totp/enroll", headers=headers, json={"password": PASSWORD})
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["provisioning_uri"].split("secret=")[1].split("&")[0]
    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    assert (
        client.post(
            "/api/v1/auth/totp/confirm", headers=headers, json={"code": generator.now()}
        ).status_code
        == 204
    )
    next_code = generator.at(datetime.now(UTC) + timedelta(seconds=totp.PERIOD_SECONDS))
    return str(_login(client, username, totp_code=next_code)["token"])


def test_nobody_can_widen_their_own_branch_scope(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    user_admin_role: str,
    api_branches: list[str],
) -> None:
    """Người nắm `system.user.edit` **không** tự gán được chi nhánh mình chưa thấy.

    Kịch bản thật: doanh nghiệp nhiều chi nhánh giao quyền quản trị tài khoản cho
    trưởng phòng từng chi nhánh. Không có luật này, người đó tự cấp mình mọi chi
    nhánh còn lại trong một request — và dòng nhật ký của thao tác đó mang
    `branch_id` của chi nhánh **đích**, tức là vô hình với chính người vừa bị
    vượt qua.
    """
    user = user_factory("tunoi")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=user_admin_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    role_service.assign_branch(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        branch_code=api_branches[0],
        actor_user_id=user.id,
        actor_branch_ids=None,
    )

    headers = _auth(_full_session(client, user.username), dataset_alpha.code)
    before = client.get("/api/v1/system/access", headers=headers).json()["branch_ids"]

    response = client.post(
        f"/api/v1/system/users/{user.id}/branches",
        headers=headers,
        json={"branch_code": api_branches[1]},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"

    after = client.get("/api/v1/system/access", headers=headers).json()["branch_ids"]
    assert after == before

    # Đối chứng: chi nhánh **đang thấy** thì gán được — luật không chặn việc thật.
    allowed = client.post(
        f"/api/v1/system/users/{user.id}/branches",
        headers=headers,
        json={"branch_code": api_branches[0]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["changed"] is False


def test_assigning_to_a_user_that_does_not_exist_is_a_404(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    user_admin_role: str,
    api_branches: list[str],
) -> None:
    """`user_branches.user_id` không có khóa ngoại (bảng khác schema) — phải tự kiểm.

    Hại thật không phải một id bịa mà là một id **gõ nhầm nhưng có thật**: cấp
    quyền cho nhầm người, im lặng, và người quản trị tin rằng đã cấp đúng. Không
    kiểm thì endpoint trả `200 {"changed": true}` và để lại một dòng mồ côi.
    """
    user = user_factory("khongcothat")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=user_admin_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    role_service.assign_branch(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        branch_code=api_branches[0],
        actor_user_id=user.id,
        actor_branch_ids=None,
    )
    headers = _auth(_full_session(client, user.username), dataset_alpha.code)

    ghost = client.post(
        "/api/v1/system/users/999777555/branches",
        headers=headers,
        json={"branch_code": api_branches[0]},
    )
    assert ghost.status_code == 404
    assert ghost.json()["error_code"] == "auth.user_not_found"


# --------------------------------------------------------------------------
# Hai hàng rào từng không có test nào canh
# --------------------------------------------------------------------------


def test_reading_the_audit_log_needs_its_own_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    user_admin_role: str,
) -> None:
    """Nhật ký là dữ liệu nhạy cảm nhất trong dataset — quyền riêng, không kèm."""
    user = user_factory("khongdocnhatky")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=user_admin_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    headers = _auth(_full_session(client, user.username), dataset_alpha.code)

    response = client.get("/api/v1/system/audit-log", headers=headers)
    assert response.status_code == 403
    assert response.json()["details"]["permission"] == AUDIT_VIEW


def test_enrolling_a_second_factor_requires_the_current_password(
    client: TestClient, user_factory: UserFactory
) -> None:
    """Hàng rào duy nhất giữa token bỏ quên và việc chiếm tài khoản vĩnh viễn.

    Ai chiếm được một phiên đang mở mà đăng ký được thiết bị 2FA **của mình** thì
    giữ tài khoản đó kể cả sau khi chủ nhân đổi mật khẩu.
    """
    user = user_factory("enroll_sai_matkhau")
    headers = _auth(str(_login(client, user.username)["token"]))

    response = client.post(
        "/api/v1/auth/totp/enroll", headers=headers, json={"password": "Sai#MatKhau2026"}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "auth.invalid_credentials"

    from ket.kernel.persistence.session import control_session

    with control_session(client.app.state.session_factory) as session:  # type: ignore[attr-defined]
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        # Cùng bất biến với change-password: cửa sau cũng phải đếm.
        assert reloaded.failed_login_count == 1
        assert reloaded.totp_secret_enc is None


ROLE_EDIT = permission_code(SYSTEM_MODULE, "role", Action.EDIT)


@pytest.fixture
def role_admin_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Vai trò chỉ có `system.role.edit` — đủ mở endpoint gán vai trò, không hơn."""
    code = "quan_tri_vai_tro_api"
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            role = Role(code=code, name="Quản trị vai trò (test)")
            session.add(role)
            session.flush()
            permission_id = session.scalar(
                select(Permission.id).where(Permission.code == ROLE_EDIT)
            )
            assert permission_id is not None
            session.add(RolePermission(role_id=role.id, permission_id=permission_id, allow=True))
    return code


def test_nobody_can_grant_a_role_wider_than_their_own_permissions(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    role_admin_role: str,
) -> None:
    """Người nắm `system.role.edit` **không** tự gán được vai trò rộng hơn mình.

    Chiều quyền của `test_nobody_can_widen_their_own_branch_scope`: không có
    chốt này, bất kỳ ai được giao "quản lý phân quyền" tự cấp `admin` cho chính
    mình trong một request — và dòng nhật ký không phân biệt được với một lần
    gán hợp lệ. Hôm nay v1 chỉ gieo một vai `admin` nên chưa ai khai thác được;
    chốt phải có **trước** khi phase 5 mang vai trò tùy biến đầu tiên.
    """
    user = user_factory("tunang_vaitro")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=role_admin_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )

    headers = _auth(_full_session(client, user.username), dataset_alpha.code)

    refused = client.post(
        f"/api/v1/system/users/{user.id}/roles",
        headers=headers,
        json={"role_code": "admin"},
    )
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "auth.role_grant_too_wide"

    # Đối chứng dương: phân phát đúng phần quyền mình đang có thì vẫn được.
    colleague = user_factory("dong_nghiep_vaitro")
    allowed = client.post(
        f"/api/v1/system/users/{colleague.id}/roles",
        headers=headers,
        json={"role_code": role_admin_role},
    )
    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["changed"] is True
