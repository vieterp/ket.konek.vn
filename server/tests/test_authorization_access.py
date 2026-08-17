"""Phân giải quyền per-dataset và đường gán vai trò (FR-SYS-071/072, FR-NFR-016).

Chạy trên PostgreSQL thật vì ba thứ đang được kiểm đều là hành vi của DB: bảng
phân quyền nằm **trong schema dataset** (nên phải `SET ROLE` + `search_path` mới
đọc được), dòng nhật ký phải đi cùng transaction, và thứ tự fail-safe khi gán
vai trò nhạy cảm chỉ quan sát được khi có hai transaction thật.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.control_log import ControlAuditAction, ControlAuditLog
from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    BranchNotFoundError,
    DatasetAccessDeniedError,
    PermissionDeniedError,
    RoleNotFoundError,
)
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import control_session, dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.authorization import Access, resolve_access
from ket.kernel.security.models import Branch, Permission, Role, RolePermission
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code

pytestmark = pytest.mark.db

UserFactory = Callable[..., User]

BRANCH_VIEW = permission_code(SYSTEM_MODULE, "branch", Action.VIEW)
BRANCH_DELETE = permission_code(SYSTEM_MODULE, "branch", Action.DELETE)
ROLE_EDIT = permission_code(SYSTEM_MODULE, "role", Action.EDIT)


@pytest.fixture
def alpha_scope(dataset_alpha: DatasetRef) -> Callable[[int], RequestScope]:
    """Ngữ cảnh ghi trong dataset `alpha` cho một người thực hiện bất kỳ."""

    def make(actor_user_id: int) -> RequestScope:
        return RequestScope(
            dataset_schema=dataset_alpha.schema_name, user_id=actor_user_id, branch_ids=()
        )

    return make


@pytest.fixture
def read_access(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> Callable[[int], Access]:
    """Đọc quyền hiệu lực của một người dùng trong `alpha`."""

    def read(user_id: int) -> Access:
        with dataset_session(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            branch_ids=(),
            audit=AuditContext(user_id=user_id),
        ) as session:
            return resolve_access(session, user_id=user_id)

    return read


def _make_role(
    factory: sessionmaker[Session],
    scope: RequestScope,
    *,
    code: str,
    grants: dict[str, bool],
) -> None:
    """Vai trò tùy biến: `grants` là mã quyền → `allow`.

    Có `allow=False` là chủ đích — luật "cấm thắng cho phép" chỉ kiểm được khi
    dựng được một vai trò cấm tường minh.
    """
    with unit_of_work(factory, scope) as session:
        role = Role(code=code, name=code)
        session.add(role)
        session.flush()
        for permission_code_value, allow in grants.items():
            permission_id = session.scalar(
                select(Permission.id).where(Permission.code == permission_code_value)
            )
            assert permission_id is not None, f"chưa gieo mã quyền {permission_code_value}"
            session.add(RolePermission(role_id=role.id, permission_id=permission_id, allow=allow))


def _grant(
    factory: sessionmaker[Session], dataset: DatasetRef, *, user_id: int, role_code: str
) -> bool:
    return role_service.grant_role(
        factory,
        dataset_schema=dataset.schema_name,
        user_id=user_id,
        role_code=role_code,
        actor_user_id=user_id,
        client_info="pytest",
    )


def _reload(factory: sessionmaker[Session], user_id: int) -> User:
    with control_session(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return user


# --------------------------------------------------------------------------
# Gieo mầm — dữ liệu kế toán mới phải dùng được ngay
# --------------------------------------------------------------------------


def test_a_new_dataset_arrives_with_permissions_and_an_admin_role(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Không gieo mầm thì dữ liệu kế toán vừa tạo là cái hộp không ai mở được."""
    result = role_service.ensure_admin_role(
        owner_engine.connect().execution_options(), dataset_alpha.schema_name
    )
    # Lần chạy thứ hai không thêm gì: `provision_dataset` đã gieo.
    assert result.permissions_added == 0
    assert result.role_permissions_added == 0
    assert result.role_created is False


def test_seeding_is_repeatable_and_grants_newly_registered_codes(
    owner_engine: Engine, dataset_beta: DatasetRef
) -> None:
    """Phase sau thêm loại chứng từ → `ensure-cluster` phải cấp mã mới cho `admin`.

    Không có đường này, quản trị viên của một doanh nghiệp tạo từ phiên bản
    trước sẽ không mở được màn hình vừa thêm — và triệu chứng là một `403` không
    ai giải thích được.
    """
    from ket.kernel.security.permissions import DocumentType, PermissionRegistry

    registry = PermissionRegistry()
    registry.register(
        DocumentType(module="cash_book", code="receipt", actions=frozenset({Action.POST}))
    )

    with owner_engine.begin() as connection:
        first = role_service.ensure_admin_role(
            connection, dataset_beta.schema_name, registry=registry
        )
        second = role_service.ensure_admin_role(
            connection, dataset_beta.schema_name, registry=registry
        )

    assert first.permissions_added == 1
    assert first.role_permissions_added >= 1
    assert (second.permissions_added, second.role_permissions_added) == (0, 0)


# --------------------------------------------------------------------------
# Phân giải quyền
# --------------------------------------------------------------------------


def test_a_user_without_any_role_is_told_they_do_not_belong_here(
    read_access: Callable[[int], Access], user_factory: UserFactory
) -> None:
    """Khác "thiếu quyền": client dùng mã này để ẩn hẳn dataset khỏi danh sách."""
    user = user_factory("khongvaitro")
    with pytest.raises(DatasetAccessDeniedError):
        read_access(user.id)


def test_admin_role_carries_every_registered_permission(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    read_access: Callable[[int], Access],
    user_factory: UserFactory,
) -> None:
    user = user_factory("quantri")
    assert _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin") is True

    access = read_access(user.id)
    assert BRANCH_VIEW in access.permissions
    assert ROLE_EDIT in access.permissions


def test_an_explicit_deny_beats_an_allow_from_another_role(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    alpha_scope: Callable[[int], RequestScope],
    read_access: Callable[[int], Access],
    user_factory: UserFactory,
) -> None:
    """ "Kế toán viên nhưng không được xóa" phải là một vai trò **cấm thêm**.

    Nếu cho phép thắng, cách duy nhất để hạn chế một hành vi là nhân bản vai trò
    gốc rồi bỏ bớt dòng — và bản sao sẽ trôi khỏi bản gốc ở lần sửa quyền sau.
    """
    user = user_factory("bicam")
    _make_role(
        session_factory,
        alpha_scope(user.id),
        code=f"cam_xoa_{user.id}",
        grants={BRANCH_DELETE: False},
    )

    assert _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin")
    assert _grant(session_factory, dataset_alpha, user_id=user.id, role_code=f"cam_xoa_{user.id}")

    access = read_access(user.id)
    assert BRANCH_DELETE not in access.permissions
    # Quyền còn lại của `admin` không bị ảnh hưởng — cấm phải hẹp đúng một mã.
    assert BRANCH_VIEW in access.permissions


def test_require_names_the_missing_permission(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    alpha_scope: Callable[[int], RequestScope],
    read_access: Callable[[int], Access],
    user_factory: UserFactory,
) -> None:
    """Quản trị viên đọc thông điệp là biết phải cấp gì."""
    user = user_factory("thieuquyen")
    _make_role(
        session_factory,
        alpha_scope(user.id),
        code=f"chi_xem_{user.id}",
        grants={BRANCH_VIEW: True},
    )
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code=f"chi_xem_{user.id}")

    access = read_access(user.id)
    with pytest.raises(PermissionDeniedError) as error:
        access.require(ROLE_EDIT)
    assert error.value.details["permission"] == ROLE_EDIT


def test_branch_scope_comes_from_user_branches(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    alpha_scope: Callable[[int], RequestScope],
    read_access: Callable[[int], Access],
    user_factory: UserFactory,
    branch_codes: tuple[str, str],
) -> None:
    """Chưa gán chi nhánh nào = **không thấy dòng nào**, không phải "thấy tất"."""
    user = user_factory("phamvichinhanh")
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin")
    assert read_access(user.id).branch_ids == ()

    for code in branch_codes:
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            user_id=user.id,
            branch_code=code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )

    assert len(read_access(user.id).branch_ids) == 2


# --------------------------------------------------------------------------
# Gán vai trò — thứ tự fail-safe với cờ 2FA
# --------------------------------------------------------------------------


def test_a_sensitive_role_turns_on_two_factor_before_the_role_is_written(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hỏng giữa chừng phải để lại "đòi 2FA mà chưa có quyền", không phải ngược lại.

    Đây là bất biến trung tâm của `role_service`: thứ tự ngược lại tạo ra một
    tài khoản quản trị **không** bị đòi lớp thứ hai, và không có gì báo.
    """
    user = user_factory("nhaycam")
    assert _reload(session_factory, user.id).totp_required is False

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ghi vai trò hỏng")

    monkeypatch.setattr("ket.kernel.security.role_service.unit_of_work", explode)
    with pytest.raises(RuntimeError, match="ghi vai trò hỏng"):
        _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin")

    assert _reload(session_factory, user.id).totp_required is True
    monkeypatch.undo()
    # Và người dùng vẫn chưa có vai trò nào — cờ bật "thừa", đúng chiều an toàn.
    with pytest.raises(DatasetAccessDeniedError):
        with dataset_session(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            branch_ids=(),
            audit=AuditContext(user_id=user.id),
        ) as session:
            resolve_access(session, user_id=user.id)


def test_turning_on_two_factor_is_recorded(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, user_factory: UserFactory
) -> None:
    """Một tài khoản bỗng bị đòi 2FA phải giải thích được bằng nhật ký."""
    user = user_factory("vetco")
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin")

    with control_session(session_factory) as session:
        actions = list(
            session.scalars(
                select(ControlAuditLog.action)
                .where(ControlAuditLog.subject_user_id == user.id)
                .order_by(ControlAuditLog.id)
            )
        )
    assert ControlAuditAction.USER_UPDATED.value in actions


def test_a_role_without_sensitive_permissions_does_not_force_two_factor(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    alpha_scope: Callable[[int], RequestScope],
    user_factory: UserFactory,
) -> None:
    """Bắt 2FA cho mọi vai trò là cách nhanh nhất để người dùng tìm đường vòng."""
    user = user_factory("chixem")
    _make_role(
        session_factory,
        alpha_scope(user.id),
        code=f"xem_thoi_{user.id}",
        grants={BRANCH_VIEW: True},
    )
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code=f"xem_thoi_{user.id}")

    assert _reload(session_factory, user.id).totp_required is False


def test_a_denied_sensitive_permission_does_not_force_two_factor(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    alpha_scope: Callable[[int], RequestScope],
    user_factory: UserFactory,
) -> None:
    """Vai trò **cấm** `system.role.edit` không phải vai trò quản trị."""
    user = user_factory("camquantri")
    _make_role(
        session_factory,
        alpha_scope(user.id),
        code=f"cam_quantri_{user.id}",
        grants={ROLE_EDIT: False, BRANCH_VIEW: True},
    )
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code=f"cam_quantri_{user.id}")

    assert _reload(session_factory, user.id).totp_required is False


def test_granting_twice_changes_nothing_the_second_time(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, user_factory: UserFactory
) -> None:
    """Màn hình phân quyền là nơi người ta bấm hai lần vì không chắc lần đầu đã ăn."""
    user = user_factory("gan2lan")
    assert _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin") is True
    assert _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin") is False


def test_revoking_a_role_leaves_two_factor_on(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, user_factory: UserFactory
) -> None:
    """Cờ toàn cục, vai trò per-dataset: tắt cờ ở đây có thể gỡ bảo vệ mà người
    dùng vẫn còn vai trò quản trị ở một dữ liệu kế toán khác."""
    user = user_factory("govaitro")
    _grant(session_factory, dataset_alpha, user_id=user.id, role_code="admin")

    assert (
        role_service.revoke_role(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            user_id=user.id,
            role_code="admin",
            actor_user_id=user.id,
        )
        is True
    )
    assert _reload(session_factory, user.id).totp_required is True


def test_unknown_role_and_branch_codes_are_business_errors(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, user_factory: UserFactory
) -> None:
    """Lỗi nghiệp vụ có mã, không phải `IntegrityError` thô nổi lên tầng API."""
    user = user_factory("machobiet")
    with pytest.raises(RoleNotFoundError):
        _grant(session_factory, dataset_alpha, user_id=user.id, role_code="khong_co_vai_tro_nay")
    with pytest.raises(BranchNotFoundError):
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            user_id=user.id,
            branch_code="khong_co_chi_nhanh",
            actor_user_id=user.id,
            actor_branch_ids=None,
        )


@pytest.fixture(scope="module")
def branch_codes(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    """Hai chi nhánh dùng chung cho các test cần phạm vi.

    Phạm vi `module` chứ `function`: `branches` có ràng buộc `code` duy nhất và
    dòng đã tạo thì không xóa (nhật ký trỏ tới nó), nên tạo lại mỗi test sẽ đụng
    ràng buộc.
    """
    assert request is not None
    codes = ("CN_AUTH_1", "CN_AUTH_2")
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        existing = set(session.scalars(select(Branch.code).where(Branch.code.in_(codes))).all())
        for code in codes:
            if code not in existing:
                BranchService(session).create(code=code, name=f"Chi nhánh {code}")
    yield codes
