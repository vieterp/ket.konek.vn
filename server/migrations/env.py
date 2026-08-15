"""Alembic environment.

Một dữ liệu kế toán = một PG schema (ADR-017). Migration vì thế chạy **theo
từng schema dataset**, không phải một lần cho cả DB: `version_table_schema`
được đặt bằng schema đích để mỗi dataset giữ lịch sử migration riêng.

Schema đích lấy từ (theo thứ tự): `Config.attributes["dataset_schema"]` (khi
gọi trong tiến trình, xem `kernel/datasets/provisioning.py`) → `-x schema=<tên>`
trên dòng lệnh → `KONEK_DEFAULT_DATASET_SCHEMA` → mặc định trong `konek.settings`.

`target_metadata` chỉ gồm bảng của **schema dataset**. Ba bảng điều khiển
(`public.datasets`, `public.users`, `public.system_metadata`) do
`kernel/datasets/bootstrap.py` dựng và cố tình không nằm ở đây — nếu có, mỗi
lần chạy migration cho một dataset, Alembic sẽ thấy chúng "thiếu" trong schema
đích và sinh lệnh tạo lại.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from konek.kernel.datasets.naming import validate_schema_name
from konek.model_registry import DatasetBase
from konek.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = DatasetBase.metadata


def _resolve_schema() -> str:
    """Schema dataset đích của lần chạy migration này (đã kiểm hợp lệ).

    Tên schema đi thẳng vào câu lệnh SQL (identifier không tham số hóa được),
    nên nó phải được kiểm trước khi dùng. Bộ luật kiểm dùng chung với runtime
    (`kernel/datasets/naming.py`) — hai bộ luật khác nhau cho cùng một tên
    schema là cách chắc chắn nhất để migration chạy nhầm chỗ.
    """
    from_attributes = config.attributes.get("dataset_schema")
    if isinstance(from_attributes, str):
        return validate_schema_name(from_attributes)

    override = context.get_x_argument(as_dictionary=True).get("schema")
    schema = str(override) if override else get_settings().default_dataset_schema
    return validate_schema_name(schema)


def _resolve_url() -> str:
    """DSN kết nối. Không đọc từ alembic.ini để credential không nằm trong repo."""
    return config.get_main_option("sqlalchemy.url") or get_settings().owner_database_url


def _configure(connection: Connection | None, schema: str) -> None:
    """Tham số chung cho cả hai chế độ.

    `include_schemas=False` + `search_path` đã trỏ đúng schema: Alembic chỉ
    nhìn thấy schema đích, nên so sánh autogenerate không lôi bảng của dataset
    khác hay của schema điều khiển vào.
    """
    common = {
        "target_metadata": target_metadata,
        "include_schemas": False,
        "compare_type": True,
        "compare_server_default": True,
    }
    if connection is None:
        # Chế độ offline không có `search_path` để dựa vào, nên phải nêu schema
        # của bảng phiên bản một cách tường minh.
        context.configure(
            url=_resolve_url(),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            version_table_schema=schema,
            **common,
        )
    else:
        # Chế độ online **không** đặt `version_table_schema`: `search_path` đã
        # trỏ vào schema dataset nên `alembic_version` rơi đúng chỗ. Đặt thêm
        # tham số đó lại làm autogenerate không nhận ra bảng phiên bản là của
        # chính nó và sinh lệnh `DROP TABLE alembic_version`.
        context.configure(connection=connection, **common)


def run_migrations_offline() -> None:
    """Sinh SQL ra stdout, không kết nối DB."""
    schema = _resolve_schema()
    _configure(None, schema)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Chạy migration trên một connection.

    Ưu tiên connection do người gọi truyền vào (`Config.attributes`): khi tạo
    dữ liệu kế toán mới, app đã có sẵn engine `konek_owner` và không nên mở
    thêm connection thứ hai bằng credential lấy từ chỗ khác.
    """
    schema = _resolve_schema()
    provided = config.attributes.get("connection")

    if isinstance(provided, Connection):
        provided.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        _configure(provided, schema)
        context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # Mọi DDL của lần chạy này nằm trong schema dataset đích.
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        _configure(connection, schema)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
