"""Đường phân quyền cũng là **đọc–sửa–ghi**, và cũng cần khóa hàng.

Cùng loại lỗi mà `test_auth_concurrency.py` canh ở luồng đăng nhập, lặp lại ở
lát sau tại một chỗ khác — đó là lý do bộ test này tồn tại riêng thay vì gộp
vào test hành vi. Đo trên cụm thật **trước** khi sửa:

    4 luồng cùng `grant_role(user, admin)`      → 2 lỗi UniqueViolation
    4 luồng cùng `assign_branch(user, CN)`      → 3 lỗi UniqueViolation
    4 connection cùng gieo mầm schema trống     → 3 lỗi UniqueViolation

`UniqueViolation` không phải `DomainError` nên nó thành **HTTP 500** — phá đúng
lời hứa vừa viết ở `GrantResponse` ("gọi lại không phải lỗi"). Màn hình phân
quyền là nơi người ta bấm hai lần vì không chắc lần đầu đã ăn chưa.

Chạy thật sự song song bằng thread + `Barrier` để bốn transaction chắc chắn
chồng lên nhau; `NullPool` nên mỗi thread có connection riêng.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch, UserBranch, UserRole
from ket.settings import Settings

pytestmark = pytest.mark.db

UserFactory = Callable[..., User]
THREADS = 4


def _run_together(job: Callable[[], object], threads: int = THREADS) -> list[BaseException]:
    """Chạy `job` đồng thời, trả về danh sách ngoại lệ thoát ra ngoài.

    `Barrier` là phần bắt buộc: không có nó, bốn lời gọi nối đuôi nhau và test
    xanh ngay cả khi khóa hàng đã bị gỡ.
    """
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            job()
        except BaseException as error:
            with lock:
                errors.append(error)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for future in [pool.submit(attempt) for _ in range(threads)]:
            future.result()
    return errors


def test_granting_the_same_role_concurrently_never_errors(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
) -> None:
    """Bốn lần gán song song → không lỗi, và đúng **một** dòng `user_roles`."""
    user = user_factory("dua_vaitro")

    errors = _run_together(
        lambda: role_service.grant_role(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            user_id=user.id,
            role_code="admin",
            actor_user_id=user.id,
        )
    )

    assert errors == [], f"gán vai trò song song vẫn ném: {errors}"
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=user.id, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        rows = session.scalars(select(UserRole).where(UserRole.user_id == user.id)).all()
    assert len(rows) == 1


def test_assigning_the_same_branch_concurrently_never_errors(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
) -> None:
    """Cùng bất biến cho `user_branches`."""
    user = user_factory("dua_chinhanh")
    code = "CN_DUA"
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=user.id, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Branch.id).where(Branch.code == code)) is None:
            BranchService(session).create(code=code, name="Chi nhánh đua")

    errors = _run_together(
        lambda: role_service.assign_branch(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            user_id=user.id,
            branch_code=code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )
    )

    assert errors == [], f"gán chi nhánh song song vẫn ném: {errors}"
    with unit_of_work(session_factory, scope) as session:
        rows = session.scalars(select(UserBranch).where(UserBranch.user_id == user.id)).all()
    assert len(rows) == 1


def test_seeding_a_fresh_dataset_concurrently_never_errors(
    test_settings: Settings, owner_engine: Engine
) -> None:
    """Gieo mầm song song trên schema **trống** — ca duy nhất từng hỏng.

    Đường thật để hai lời gọi gặp nhau: `provision_dataset` tạo dữ liệu kế toán
    mới trong lúc ai đó chạy `ket.admin ensure-cluster`. Chạy trên schema đã
    gieo rồi thì không lỗi, nên bug chỉ lộ ra đúng lần đầu — tức là ở nơi cài
    đặt, không phải trên máy lập trình.

    Mỗi thread một engine riêng: `owner_engine` dùng chung sẽ khiến bốn thread
    chờ nhau ở tầng pool và che mất chính thứ đang đo.
    """
    schema = "ds_seed_race"
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    # Bảng phân quyền phải có trước khi gieo; mượn migration của dataset thật là
    # quá đắt cho một test đua, nên chép đúng ba bảng mà `ensure_admin_role` chạm.
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        connection.exec_driver_sql(
            "CREATE TABLE permissions (id SERIAL PRIMARY KEY, "
            "code VARCHAR(120) NOT NULL CONSTRAINT uq_permissions_code UNIQUE)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE roles (id SERIAL PRIMARY KEY, "
            "code VARCHAR(50) NOT NULL CONSTRAINT uq_roles_code UNIQUE, "
            "name VARCHAR(255) NOT NULL, name_en VARCHAR(255), "
            "is_system BOOLEAN NOT NULL DEFAULT false)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE role_permissions (role_id INTEGER NOT NULL, "
            "permission_id INTEGER NOT NULL, allow BOOLEAN NOT NULL DEFAULT true, "
            "PRIMARY KEY (role_id, permission_id))"
        )

    def seed() -> None:
        engine = create_engine(test_settings.owner_database_url, poolclass=NullPool)
        try:
            with engine.begin() as connection:
                role_service.ensure_admin_role(connection, schema)
        finally:
            engine.dispose()

    errors = _run_together(seed)

    try:
        assert errors == [], f"gieo mầm song song vẫn ném: {errors}"
        with owner_engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            roles = connection.exec_driver_sql("SELECT count(*) FROM roles").scalar_one()
        assert roles == 1
    finally:
        with owner_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
