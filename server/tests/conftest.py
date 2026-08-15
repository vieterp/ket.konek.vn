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
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from ket.kernel.datasets.bootstrap import ensure_control_schema, ensure_database_roles
from ket.kernel.datasets.provisioning import DatasetRef, provision_dataset
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.security.dataset_roles import CONTROL_GROUP_ROLE
from ket.kernel.security.grants import APP_ROLE, OWNER_ROLE
from ket.kernel.security.rls import validate_identifier
from ket.settings import Settings

SERVER_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_ROOT = SERVER_ROOT / "src" / "ket"

TEST_DATABASE = "ket_test"
DEFAULT_ADMIN_DSN = "postgresql+psycopg://localhost/postgres"

DESTRUCTIVE_CLUSTER_ENV = "KET_TEST_DESTRUCTIVE_CLUSTER"
"""Khẳng định cụm PostgreSQL đang trỏ tới là cụm dùng riêng cho test.

Xem `_drop_ket_roles` — nhóm test `db` xóa vai trò ở phạm vi cụm, và cụm chạy
test thường là cụm cá nhân của lập trình viên, nơi có thể có bản cài khác."""


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


def _drop_ket_roles(connection: Connection) -> None:
    """Xóa mọi vai trò của Konek trước khi dựng lại cụm test.

    Vai trò là đối tượng **cấp cụm**, không nằm trong database — nên
    `DROP DATABASE` không đụng tới chúng. Không xóa ở đây thì mỗi phiên test
    chạy trên vai trò do phiên **trước** tạo ra, với thuộc tính của phiên trước.

    Đây không phải chuyện dọn dẹp cho gọn. Nó từng khiến kiểm đột biến cho ra
    kết quả sai: gỡ `NOINHERIT` khỏi `roles.sql` mà hai test cô lập dataset vẫn
    xanh, vì vai trò cũ (đã đúng) còn nguyên và `GRANT` chạy lại không sửa lựa
    chọn kế thừa của một tư cách thành viên đã tồn tại. Một cổng bảo mật chỉ
    xanh nhờ trạng thái sót lại là cổng không còn canh gì cả.

    Xóa theo thứ tự phụ thuộc: vai trò dataset trước (chúng là thành viên của
    `ket_control`), rồi tới ba vai trò nền.

    **Đòi opt-in tường minh.** Đây là DDL phá hủy phạm vi **cụm**, không phải
    phạm vi database test: một bản cài Két thật trên cùng cụm mà chưa tạo bảng
    nào sẽ bị xóa mất vai trò và chết, im lặng. `DROP ROLE` tự chặn khi vai trò
    còn đối tượng phụ thuộc, nhưng "chưa có đối tượng nào" đúng là trạng thái
    của một bản cài vừa chạy `roles.sql` — tức là lưới an toàn của PostgreSQL
    hụt đúng ở ca nguy nhất. Một biến môi trường buộc người chạy khẳng định cụm
    này là cụm dùng riêng cho test.
    """
    if not os.environ.get(DESTRUCTIVE_CLUSTER_ENV):
        pytest.fail(
            f"Nhóm test `db` xóa và dựng lại vai trò ở phạm vi CỤM. Đặt "
            f"{DESTRUCTIVE_CLUSTER_ENV}=1 để khẳng định cụm tại "
            f"{_admin_dsn()} là cụm dùng riêng cho test (`make server-test-db` đã đặt sẵn)."
        )
    dataset_roles = [
        row[0]
        for row in connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'ds\\_%\\_app'")
        ).all()
    ]
    roles = [*dataset_roles, APP_ROLE, CONTROL_GROUP_ROLE, OWNER_ROLE]

    # Biến môi trường trên là lời khẳng định của con người, và `Makefile` đặt sẵn
    # nó — nên nó chỉ chặn được người gõ `pytest -m db` trần. Đây mới là kiểm
    # thật: `pg_shdepend` cho biết vai trò còn được database NÀO tham chiếu. Còn
    # bản cài khác dùng chung cụm thì dừng lại, với thông điệp chỉ đúng chỗ —
    # thay vì để `DROP ROLE` đổ giữa chừng bằng "19 objects in database …" và
    # kéo theo 56 test lỗi không liên quan.
    outsiders = (
        connection.execute(
            text(
                "SELECT DISTINCT d.datname FROM pg_shdepend s "
                "JOIN pg_database d ON d.oid = s.dbid "
                "JOIN pg_authid a ON a.oid = s.refobjid "
                "WHERE a.rolname = ANY(:roles) AND d.datname <> :test_db"
            ),
            {"roles": roles, "test_db": TEST_DATABASE},
        )
        .scalars()
        .all()
    )
    if outsiders:
        pytest.fail(
            f"Cụm tại {_admin_dsn()} còn database dùng vai trò Konek: {sorted(outsiders)}. "
            "Nhóm test `db` sẽ xóa các vai trò đó và làm hỏng bản cài kia. "
            "Trỏ KET_TEST_ADMIN_DSN sang một cụm dùng riêng cho test."
        )

    for role in roles:
        validate_identifier(role)
        connection.exec_driver_sql(
            f"DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'DROP OWNED BY {role}'; EXECUTE 'DROP ROLE {role}'; "
            f"END IF; END $$"
        )


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
        _drop_ket_roles(connection)
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
def pooled_app_engine(test_settings: Settings) -> Iterator[Engine]:
    """Engine runtime dùng **pool thật**, đúng một connection.

    Mọi engine khác ở đây dùng `NullPool` (mỗi lần dùng một connection mới) —
    hợp lý cho test cô lập, nhưng nó khiến một lớp bất biến **không quan sát
    được**: `SET LOCAL ROLE`, `SET LOCAL search_path` và GUC chi nhánh phải hết
    hiệu lực khi connection quay lại pool. Không có pool thì không có "quay lại
    pool", nên đổi `SET LOCAL` thành `SET` vẫn xanh toàn bộ bộ test — chính là
    lỗ mà kiểm đột biến M16 chỉ ra.

    `pool_size=1, max_overflow=0`: hai transaction liên tiếp **chắc chắn** dùng
    chung một connection vật lý, nên rò trạng thái là quan sát được chứ không
    phụ thuộc may rủi.
    """
    engine = create_engine(
        test_settings.database_url, poolclass=QueuePool, pool_size=1, max_overflow=0
    )
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
