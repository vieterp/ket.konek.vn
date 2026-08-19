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
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from ket import __version__
from ket.api.middleware.schema_version_gate import CLIENT_VERSION_HEADER
from ket.kernel.datasets.bootstrap import ensure_control_schema, ensure_database_roles
from ket.kernel.datasets.models import User
from ket.kernel.datasets.naming import validate_schema_name
from ket.kernel.datasets.provisioning import DatasetRef, provision_dataset
from ket.kernel.persistence.session import control_session, create_session_factory
from ket.kernel.security.account_service import create_user
from ket.kernel.security.dataset_roles import CONTROL_GROUP_ROLE, WORKER_ROLE
from ket.kernel.security.grants import APP_ROLE, OWNER_ROLE
from ket.kernel.security.keystore import SecretBox, generate_app_key
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
def admin_dsn() -> str:
    """DSN superuser tới database `postgres` của cụm test.

    Lộ ra cho test cần **tạo database riêng** — diễn tập nâng cấp schema điều
    khiển phải chạy trên một database dùng một lần, vì nó hạ cấp rồi nâng lại
    chính `public`. Làm việc đó trên database test dùng chung sẽ xóa cột của các
    bảng mà test khác đang dựa vào.
    """
    return _admin_dsn()


@pytest.fixture(scope="session")
def owner_dsn() -> str:
    """DSN của `ket_owner` tới database test.

    Test dựng database dùng một lần lấy DSN này rồi **đổi tên database** ở cuối
    chuỗi, thay vì tự ghép từ `admin_dsn`: DSN của CI mang theo tên đăng nhập
    (`postgres@localhost`), nên ghép tay sẽ cho ra `ket_owner@postgres@localhost`
    — hỏng ở CI mà xanh trên máy lập trình.
    """
    return _dsn_for("ket_owner")


@pytest.fixture(scope="session")
def app_key() -> str:
    """Khóa Fernet dùng chung cho test cần mã hóa bí mật (ADR-019).

    Sinh trong bộ nhớ, không đụng OS keystore: test không được phép ghi vào
    Keychain/Credential Manager của người đang chạy nó.
    """
    return generate_app_key()


@pytest.fixture(scope="session")
def secret_box(app_key: str) -> SecretBox:
    """Hộp mã hóa dùng chung cho test 2FA."""
    return SecretBox(app_key.encode("ascii"))


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
    roles = [*dataset_roles, APP_ROLE, WORKER_ROLE, CONTROL_GROUP_ROLE, OWNER_ROLE]

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
def test_settings(postgres_available: bool, app_key: str) -> Settings:
    """Cấu hình trỏ vào database test, dựng sẵn vai trò + schema điều khiển.

    Dựng lại database từ đầu mỗi phiên: test kiểm cấu trúc và quyền, nên dữ
    liệu sót lại từ phiên trước có thể làm một test xanh vì lý do sai.

    `app_key` truyền thẳng vào cấu hình thay vì để app đọc OS keystore: test
    không được phép ghi vào Keychain/Credential Manager của người đang chạy nó,
    nhưng luồng 2FA qua HTTP thì vẫn phải kiểm được đầu-cuối.
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
        worker_database_url=_dsn_for("ket_worker"),
        verify_schema_on_startup=False,
        app_key=SecretStr(app_key),
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
def worker_engine(test_settings: Settings) -> Iterator[Engine]:
    """Engine của tiến trình chạy tác vụ nền (`ket_worker`).

    Vai trò riêng chứ không dùng lại `app_engine`: quyền của worker dừng ở đúng
    bảng `jobs` và nó có một policy RLS riêng — dùng nhầm engine runtime sẽ làm
    test hàng đợi xanh trong khi worker thật không giành nổi một job nào.
    """
    engine = create_engine(test_settings.worker_database_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_session_factory(worker_engine: Engine) -> sessionmaker[Session]:
    """Nhà máy `Session` cho đường giành việc của worker."""
    return create_session_factory(worker_engine)


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


@pytest.fixture
def drain_jobs(owner_engine: Engine) -> Callable[..., None]:
    """Xóa sạch hàng đợi của một hoặc nhiều dữ liệu kế toán.

    Chạy bằng `ket_owner` chứ không phải vai trò runtime, và đó là điểm quan
    trọng: vai trò runtime bị RLS chặn theo chi nhánh, nên một `DELETE FROM jobs`
    với phạm vi rỗng chỉ xóa được dòng `branch_id IS NULL` — job của chi nhánh
    khác **sống sót** và trở thành job mà `claim_next` (lấy cũ nhất) giành ở test
    sau. Đó là một giờ đi tìm lý do test đỏ ở chỗ không liên quan.
    """

    def drain(*datasets: DatasetRef) -> None:
        with owner_engine.begin() as connection:
            for dataset in datasets:
                validate_schema_name(dataset.schema_name)
                connection.exec_driver_sql(f'DELETE FROM "{dataset.schema_name}".jobs')

    return drain


@pytest.fixture(scope="session")
def dataset_alpha(owner_engine: Engine) -> DatasetRef:
    return provision_dataset(owner_engine, code="alpha", name="Công ty Alpha", scheme="TT99")


@pytest.fixture(scope="session")
def dataset_beta(owner_engine: Engine) -> DatasetRef:
    """Dataset thứ hai — tồn tại để chứng minh hai doanh nghiệp không thấy nhau."""
    return provision_dataset(owner_engine, code="beta", name="Công ty Beta", scheme="TT133")


TEST_PASSWORD = "Ph1eu#Thu2026"
"""Mật khẩu đạt chính sách, dùng chung cho test danh tính."""


@pytest.fixture(scope="session")
def test_password() -> str:
    """Mật khẩu mặc định của `user_factory`, lộ ra dưới dạng fixture.

    Fixture chứ không phải import từ `conftest`: thư mục `tests` không phải một
    gói Python (không có `__init__.py`), nên `from tests.conftest import …` chỉ
    chạy được tùy cách gọi pytest.
    """
    return TEST_PASSWORD


@pytest.fixture
def user_factory(session_factory: sessionmaker[Session]) -> Callable[..., User]:
    """Tạo người dùng test với tên **duy nhất** cho mỗi lần gọi.

    Tên duy nhất chứ không phải dọn bảng sau mỗi test: `users` là bảng điều
    khiển toàn cục và `control_audit_log` chỉ-thêm tham chiếu tới nó, nên xóa
    người dùng giữa các test sẽ để lại nhật ký trỏ vào hư không — đúng thứ bảng
    nhật ký sinh ra để tránh.
    """

    def make(prefix: str = "user", *, password: str = TEST_PASSWORD, **kwargs: object) -> User:
        username = f"{prefix}_{uuid4().hex[:10]}"
        with control_session(session_factory) as session:
            user = create_user(
                session,
                username=username,
                password=password,
                must_change_password=False,
                client_info="pytest",
            )
            for field, value in kwargs.items():
                setattr(user, field, value)
            session.flush()
            return user

    return make


@pytest.fixture(scope="session")
def session_factory(app_engine: Engine) -> sessionmaker[Session]:
    """Nhà máy session runtime, đã gắn listener nhật ký.

    Phạm vi `session`: nhà máy không giữ trạng thái giữa các test (mỗi
    `Session` mở transaction riêng), và fixture dữ liệu mẫu phạm vi module cần
    dùng được nó.
    """
    return create_session_factory(app_engine)


def api_test_client(app: FastAPI) -> TestClient:
    """`TestClient` tự khai phiên bản client, như một bản cài thật.

    Từ bước 19, lệnh ghi thiếu `X-Client-Version` bị từ chối `426` (quyết định
    H2 — cổng fail-open chỉ chặn được client trung thực). Test API muốn kiểm
    tầng *nghiệp vụ* thì phải đi qua cổng đó trước, y như client thật.

    Đặt ở conftest chứ không chép vào từng tệp test: cổng này áp cho **mọi**
    đường ghi của mọi phase sau, và một tệp test mới quên header sẽ đỏ theo kiểu
    khó đọc (`426` ở chỗ đang chờ `403`). Ở đây có đúng một nơi để sửa.

    `raise_server_exceptions=False` để ngoại lệ không lường trước đi qua chính
    handler RFC 7807 mà bản cài thật dùng, thay vì nổi lên thành traceback của
    pytest — hợp đồng lỗi cũng là thứ đang được kiểm.
    """
    return TestClient(
        app,
        raise_server_exceptions=False,
        headers={CLIENT_VERSION_HEADER: __version__},
    )
