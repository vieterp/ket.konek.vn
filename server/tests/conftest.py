"""Fixture dùng chung.

Test đánh dấu `@pytest.mark.db` cần **PostgreSQL thật**. Không mô phỏng được:
RLS, tách quyền sở hữu bảng, `search_path`, `SELECT … FOR UPDATE SKIP LOCKED`
là hành vi của PostgreSQL, và chính chúng là thứ đang được kiểm. Một bộ test
chạy trên SQLite sẽ xanh trong khi cơ chế cô lập dữ liệu không hoạt động.

Kết nối lấy từ `KET_TEST_ADMIN_DSN` (mặc định: PostgreSQL cục bộ). Không kết
nối được thì nhóm `db` **bị bỏ qua** chứ không làm đỏ CI trên runner không có
DB — cổng thật nằm ở job CI có service PostgreSQL.

Mỗi phiên test dựng lại database từ đầu: schema điều khiển + hai dataset độc
lập (`alpha`, `beta`) để kiểm cô lập dữ liệu giữa các doanh nghiệp.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ket.kernel.datasets.bootstrap import ensure_control_schema, ensure_database_roles
from ket.kernel.datasets.provisioning import DatasetRef, provision_dataset
from ket.kernel.persistence.session import create_session_factory
from ket.settings import Settings

SERVER_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_ROOT = SERVER_ROOT / "src" / "ket"

TEST_DATABASE = "ket_test"
DEFAULT_ADMIN_DSN = "postgresql+psycopg://localhost/postgres"


@pytest.fixture(scope="session")
def domain_root() -> Path:
    """Thư mục gốc của mã nghiệp vụ (`server/src/ket`)."""
    return DOMAIN_ROOT


def _admin_dsn() -> str:
    return os.environ.get("KET_TEST_ADMIN_DSN", DEFAULT_ADMIN_DSN)


def _dsn_for(role: str) -> str:
    """DSN của một vai trò tới database test.

    Mật khẩu cố ý không có: máy lập trình dùng `trust`/`peer` cục bộ, còn CI
    truyền DSN đầy đủ qua `KET_TEST_*_DSN`.
    """
    override = os.environ.get(f"KET_TEST_{role.upper()}_DSN")
    if override:
        return override
    return f"postgresql+psycopg://{role}@localhost/{TEST_DATABASE}"


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    """Có PostgreSQL để chạy nhóm `db` không."""
    try:
        engine = create_engine(_admin_dsn(), poolclass=NullPool)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        return False
    return True


@pytest.fixture(scope="session")
def test_settings(postgres_available: bool) -> Settings:
    """Cấu hình trỏ vào database test, dựng sẵn vai trò + schema điều khiển.

    Dựng lại database từ đầu mỗi phiên: test kiểm cấu trúc và quyền, nên dữ
    liệu sót lại từ phiên trước có thể làm một test xanh vì lý do sai.
    """
    if not postgres_available:
        message = f"Không kết nối được PostgreSQL tại {_admin_dsn()}"
        # Trên máy lập trình, bỏ qua là hợp lý. Trong CI thì **không**: job này
        # tồn tại để chạy đúng nhóm test đó, và "xanh vì đã bỏ qua" là tín hiệu
        # tệ hơn không có tín hiệu — cổng bảo mật sẽ mục dần mà không ai biết.
        if os.environ.get("KET_TEST_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)

    admin = create_engine(_admin_dsn(), poolclass=NullPool).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DATABASE},
        )
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{TEST_DATABASE}"')

    settings = Settings(
        database_url=_dsn_for("ket_app"),
        owner_database_url=_dsn_for("ket_owner"),
        verify_schema_on_startup=False,
    )

    superuser_engine = create_engine(
        _admin_dsn().rsplit("/", 1)[0] + f"/{TEST_DATABASE}", poolclass=NullPool
    )
    ensure_database_roles(superuser_engine)
    superuser_engine.dispose()

    owner_engine = create_engine(settings.owner_database_url, poolclass=NullPool)
    ensure_control_schema(owner_engine)
    owner_engine.dispose()

    return settings


@pytest.fixture(scope="session")
def superuser_engine(test_settings: Settings) -> Iterator[Engine]:
    """Kết nối superuser tới database test.

    Chỉ dùng để **dựng điều kiện tấn công** mà vai trò thường không dựng được
    (ví dụ tạo bảng tạm sau khi `roles.sql` đã thu hồi quyền `TEMPORARY`), để
    kiểm từng lớp phòng thủ một cách độc lập.
    """
    engine = create_engine(_admin_dsn().rsplit("/", 1)[0] + f"/{TEST_DATABASE}", poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(test_settings: Settings) -> Iterator[Engine]:
    """Engine đặc quyền — chỉ dùng để dựng dataset và để kiểm quyền."""
    engine = create_engine(test_settings.owner_database_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(test_settings: Settings) -> Iterator[Engine]:
    """Engine runtime (`ket_app`) — mọi test hành vi phải đi qua engine này.

    Dùng nhầm `owner_engine` sẽ khiến RLS và quyền `audit_log` **không** có tác
    dụng, và test sẽ xanh trong khi cơ chế bảo vệ đã hỏng.
    """
    engine = create_engine(test_settings.database_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def dataset_alpha(owner_engine: Engine) -> DatasetRef:
    return provision_dataset(owner_engine, code="alpha", name="Công ty Alpha", scheme="TT200")


@pytest.fixture(scope="session")
def dataset_beta(owner_engine: Engine) -> DatasetRef:
    """Dataset thứ hai — tồn tại để chứng minh hai doanh nghiệp không thấy nhau."""
    return provision_dataset(owner_engine, code="beta", name="Công ty Beta", scheme="TT133")


@pytest.fixture(scope="session")
def session_factory(app_engine: Engine) -> sessionmaker[Session]:
    """Nhà máy session runtime, đã gắn listener nhật ký.

    Phạm vi `session`: nhà máy không giữ trạng thái giữa các test (mỗi
    `Session` mở transaction riêng), và fixture dữ liệu mẫu phạm vi module cần
    dùng được nó.
    """
    return create_session_factory(app_engine)
