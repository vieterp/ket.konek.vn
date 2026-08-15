"""Tạo một dữ liệu kế toán mới = tạo schema + chạy toàn bộ migration lên đó.

FR-SYS-001: nhiều doanh nghiệp / nhiều năm độc lập trong cùng một bản cài.
ADR-017 chọn schema-per-dataset thay vì nhiều database (sao lưu và nâng cấp
gọn hơn) và thay vì cột `dataset_id` trên mọi bảng (RLS, đánh số và index đơn
giản hơn, và không có cách nào quên `WHERE dataset_id = …`).

Thao tác này **đặc quyền**: chạy bằng `ket_owner`, không phải vai trò runtime.
Nó cũng chậm (chạy hết chuỗi migration), nên phase 2 slice sau gọi nó như một
job nền có tiến độ chứ không phải trong vòng đời một request HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.datasets.models import Dataset
from ket.kernel.datasets.naming import (
    role_name_for_schema,
    schema_name_for,
    validate_dataset_code,
    validate_schema_name,
)
from ket.kernel.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    DatasetRoleNotAdministrableError,
    SchemaVersionMismatchError,
)
from ket.kernel.security.dataset_roles import (
    create_dataset_role_statements,
    drop_dataset_role_statements,
    set_local_role_statement,
)
from ket.kernel.security.grants import OWNER_ROLE

ALEMBIC_SCHEMA_ATTRIBUTE = "dataset_schema"
"""Khóa truyền schema đích cho `migrations/env.py` qua `Config.attributes`.

Dùng thay cho `-x schema=…` vì `-x` chỉ tồn tại khi Alembic chạy từ dòng lệnh;
gọi trong tiến trình thì phải giả lập `cmd_opts`, và đó là API nội bộ.
"""


@dataclass(frozen=True)
class DatasetRef:
    """Tham chiếu gọn tới một dữ liệu kế toán (không kéo theo ORM/session)."""

    id: int
    code: str
    schema_name: str
    scheme: str


def find_alembic_config(explicit_path: Path | None = None) -> Config:
    """Định vị `alembic.ini`.

    Mặc định suy từ vị trí gói (`server/src/ket/...` → `server/`). Cách này
    đúng khi chạy từ mã nguồn nhưng **không** đúng khi cài từ wheel — đóng gói
    migration vào bản phát hành là việc của phase 11 (S4). Cho tới đó,
    `KET_ALEMBIC_INI_PATH` là đường thoát rõ ràng thay vì đoán mò.
    """
    path = explicit_path or Path(__file__).resolve().parents[4] / "alembic.ini"
    if not path.is_file():
        raise FileNotFoundError(
            f"Không tìm thấy alembic.ini tại {path}. Đặt KET_ALEMBIC_INI_PATH nếu chạy "
            "từ thư mục khác."
        )
    config = Config(str(path))
    config.set_main_option("script_location", str(path.parent / "migrations"))
    return config


def head_revision(config: Config) -> str:
    """Revision mà **mã nguồn** đang mong đợi."""
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise SchemaVersionMismatchError("Chưa có migration nào — thư mục versions rỗng")
    return head


def current_revision(engine: Engine, schema: str) -> str | None:
    """Revision **DB** đang ở, đọc từ `alembic_version` của chính schema đó.

    Chuyển sang vai trò của dataset trước khi đọc (D3): từ khi quyền trên schema
    dataset thuộc về `ds_<mã>_app`, vai trò đăng nhập `ket_app` không còn cả
    `USAGE` trên schema đó. Đây chính là chỗ lỗi B1 của lát 2A từng xảy ra —
    app server chạy tốt trên máy lập trình rồi chết ở nơi cài đặt ngay sau khi
    có dữ liệu kế toán đầu tiên — nên nó phải nằm trong đường kiểm khởi động,
    không phải được phát hiện lại lần nữa.
    """
    validate_schema_name(schema)
    with engine.begin() as connection:
        connection.exec_driver_sql(set_local_role_statement(schema))
        exists = connection.execute(
            text("SELECT to_regclass(:name)"), {"name": f"{schema}.alembic_version"}
        ).scalar_one_or_none()
        if exists is None:
            return None
        return connection.execute(
            text(f'SELECT version_num FROM "{schema}".alembic_version')  # noqa: S608
        ).scalar_one_or_none()


def upgrade_dataset_schema(engine: Engine, schema: str, config: Config | None = None) -> None:
    """Chạy migration lên `head` cho đúng một schema dataset.

    Truyền sẵn `connection` để Alembic dùng lại engine `ket_owner` hiện có
    thay vì tự mở connection từ `sqlalchemy.url` trong alembic.ini — credential
    không nằm trong repo (xem `migrations/env.py`).
    """
    validate_schema_name(schema)
    resolved = config or find_alembic_config()
    with engine.begin() as connection:
        resolved.attributes["connection"] = connection
        resolved.attributes[ALEMBIC_SCHEMA_ATTRIBUTE] = schema
        try:
            command.upgrade(resolved, "head")
        finally:
            resolved.attributes.pop("connection", None)
            resolved.attributes.pop(ALEMBIC_SCHEMA_ATTRIBUTE, None)

        # `alembic_version` do chính Alembic tạo nên không migration nào cấp
        # quyền cho nó. Vai trò runtime phải ĐỌC được: nó là nguồn của cả kiểm
        # phiên bản lúc khởi động (`main.verify_schema_versions`) lẫn handshake
        # với client (bước 19). Thiếu dòng này, app server dựng xong dữ liệu kế
        # toán đầu tiên là không khởi động lại được — và chỉ hỏng ở nơi cài đặt,
        # không hỏng trên máy lập trình.
        # Chỉ SELECT: ghi phiên bản là việc của `ket_owner`.
        #
        # Bên nhận là vai trò của chính dataset này (D3), không phải `ket_app`:
        # phiên đọc `alembic_version` là phiên đã `SET ROLE` vào dataset đó.
        connection.exec_driver_sql(
            f'GRANT SELECT ON "{schema}".alembic_version TO {role_name_for_schema(schema)}'
        )


def assert_dataset_role_administrable(connection: Connection, schema: str) -> None:
    """Chặn sớm khi vai trò của dataset tồn tại nhưng `ket_owner` không quản trị được.

    Vai trò chưa tồn tại thì không có gì để kiểm — `ket_owner` tạo nó và tự có
    ADMIN. Vai trò do **superuser** tạo (khôi phục `pg_dumpall --globals-only`,
    cài lại owner) thì `ALTER ROLE`/`GRANT` phía sau sẽ đổ với *permission denied
    to alter role*, ở giữa chuỗi DDL, sau khi `CREATE SCHEMA` đã chạy.
    """
    role = role_name_for_schema(schema)
    exists, administrable = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role), "
            "EXISTS (SELECT 1 FROM pg_auth_members m "
            "  JOIN pg_roles r ON r.oid = m.roleid "
            "  JOIN pg_roles g ON g.oid = m.member "
            "  WHERE r.rolname = :role AND g.rolname = :owner AND m.admin_option)"
        ),
        {"role": role, "owner": OWNER_ROLE},
    ).one()

    if exists and not administrable:
        raise DatasetRoleNotAdministrableError(
            f"Vai trò {role} đã tồn tại nhưng {OWNER_ROLE} không có ADMIN OPTION trên nó. "
            f"Chạy bằng superuser: GRANT {role} TO {OWNER_ROLE} WITH ADMIN OPTION;",
            role=role,
            schema=schema,
        )


def provision_dataset(
    owner_engine: Engine,
    *,
    code: str,
    name: str,
    scheme: str,
    config: Config | None = None,
) -> DatasetRef:
    """Tạo dữ liệu kế toán mới: schema + migration + dòng đăng ký.

    Không gói cả ba bước vào một transaction: `CREATE SCHEMA` và chuỗi
    migration là DDL dài, và Alembic tự quản transaction của nó. Thứ tự được
    chọn để trạng thái dở dang là **schema có nhưng chưa đăng ký** — vô hình với
    ứng dụng và dọn được — thay vì đăng ký trỏ tới schema rỗng, thứ mà app sẽ
    tin là dùng được.
    """
    validate_dataset_code(code)
    schema = schema_name_for(code)

    # Kiểm sớm để hỏng rẻ (không tạo schema thừa), nhưng **không** coi đây là
    # trọng tài: nó commit và đóng trước `CREATE SCHEMA`, nên hai lời gọi đồng
    # thời đều lọt qua. Trọng tài thật là ràng buộc UNIQUE ở bước đăng ký cuối.
    with Session(owner_engine) as session, session.begin():
        if session.scalar(select(Dataset).where(Dataset.code == code)) is not None:
            raise DatasetAlreadyExistsError("Mã dữ liệu kế toán đã tồn tại", code=code)

    with owner_engine.begin() as connection:
        assert_dataset_role_administrable(connection, schema)
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        connection.exec_driver_sql(f'ALTER SCHEMA "{schema}" OWNER TO {OWNER_ROLE}')
        # Vai trò phải có TRƯỚC migration: migration cấp quyền từng bảng cho nó
        # ngay cạnh `create_table` (`grants.py`), nên nó phải tồn tại từ lệnh
        # `create_table` đầu tiên.
        for statement in create_dataset_role_statements(schema):
            connection.exec_driver_sql(statement)

    upgrade_dataset_schema(owner_engine, schema, config)

    try:
        with Session(owner_engine) as session, session.begin():
            dataset = Dataset(code=code, schema_name=schema, name=name, scheme=scheme)
            session.add(dataset)
            session.flush()
            return DatasetRef(
                id=dataset.id,
                code=dataset.code,
                schema_name=dataset.schema_name,
                scheme=dataset.scheme,
            )
    except IntegrityError as error:
        # Người thua trong cuộc đua: schema đã tạo (rỗng, vô hại, dọn được) còn
        # dòng đăng ký thì không. Trả lỗi nghiệp vụ đúng nghĩa thay vì để
        # `IntegrityError` thô nổi lên tầng API.
        raise DatasetAlreadyExistsError("Mã dữ liệu kế toán đã tồn tại", code=code) from error


def drop_dataset_schema(owner_engine: Engine, code: str) -> None:
    """Xóa hẳn một dữ liệu kế toán. Chỉ dùng cho test và cho thao tác quản trị có xác nhận.

    Không có đường gọi từ API nghiệp vụ: một lệnh xóa nhầm ở đây là mất sổ sách
    của cả một doanh nghiệp.
    """
    with Session(owner_engine) as session:
        dataset = session.scalar(select(Dataset).where(Dataset.code == code))
        # Tên schema đọc từ **dòng đăng ký**, không suy lại từ mã: dòng đăng ký
        # là nguồn sự thật, và suy lại sẽ trỏ nhầm nếu quy ước đặt tên từng đổi.
        schema = validate_schema_name(dataset.schema_name) if dataset else schema_name_for(code)

    # Xóa schema TRƯỚC, gỡ đăng ký SAU. Thứ tự ngược lại để lại một schema đầy
    # sổ sách mà không còn con trỏ nào tới nó nếu lệnh DROP hỏng.
    #
    # Vai trò xóa sau schema: `DROP ROLE` bị từ chối chừng nào vai trò còn quyền
    # trên đối tượng đang tồn tại. Vai trò sót lại còn nguy hơn — mã dataset
    # dùng lại sau này sẽ trúng một vai trò mang quyền cũ.
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for statement in drop_dataset_role_statements(schema):
            connection.exec_driver_sql(statement)

    with Session(owner_engine) as session, session.begin():
        registered = session.scalar(select(Dataset).where(Dataset.code == code))
        if registered is not None:
            session.delete(registered)


def verify_dataset_schema_version(
    engine: Engine, schema: str, config: Config | None = None
) -> None:
    """Chặn khởi động khi schema dataset lệch revision với mã nguồn (LD-05, FR-NFR-054)."""
    resolved = config or find_alembic_config()
    expected = head_revision(resolved)
    found = current_revision(engine, schema)
    if found is None:
        raise DatasetNotFoundError("Schema dataset chưa chạy migration", schema=schema)
    if found != expected:
        raise SchemaVersionMismatchError(
            "Schema dataset lệch phiên bản migration với mã nguồn đang chạy",
            schema=schema,
            expected=expected,
            found=found,
        )
