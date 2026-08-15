"""Alembic environment.

Một dữ liệu kế toán = một PG schema (ADR-017). Migration vì thế chạy **theo
từng schema dataset**, không phải một lần cho cả DB: `version_table_schema`
được đặt bằng schema đích để mỗi dataset giữ lịch sử migration riêng.

Schema đích lấy từ (theo thứ tự): `-x schema=<tên>` trên dòng lệnh →
`KONEK_DEFAULT_DATASET_SCHEMA` → mặc định trong `konek.settings`.

Phase 1 chưa có model nào; `target_metadata` để `None` nên autogenerate chưa
sinh gì. Phase 2 gắn `MetaData` thật của kernel vào đây.
"""

from __future__ import annotations

import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from konek.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

# Tên schema đi thẳng vào câu lệnh SQL (identifier không tham số hóa được), nên
# nó phải được kiểm trước khi dùng — `-x schema=...` đến từ dòng lệnh và
# `KONEK_DEFAULT_DATASET_SCHEMA` đến từ môi trường. Chuẩn code cấm nối chuỗi SQL;
# đây là ngoại lệ duy nhất và nó được rào bằng whitelist ký tự.
_SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _resolve_schema() -> str:
    """Schema dataset đích của lần chạy migration này (đã kiểm hợp lệ)."""
    override = context.get_x_argument(as_dictionary=True).get("schema")
    schema = str(override) if override else get_settings().default_dataset_schema

    if not _SCHEMA_PATTERN.match(schema):
        raise ValueError(
            f"Tên schema dataset không hợp lệ: {schema!r}. "
            "Chỉ cho phép chữ thường, chữ số và dấu gạch dưới, bắt đầu bằng chữ "
            "hoặc gạch dưới, tối đa 63 ký tự."
        )

    return schema


def _resolve_url() -> str:
    """DSN kết nối. Không đọc từ alembic.ini để credential không nằm trong repo."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Sinh SQL ra stdout, không kết nối DB."""
    schema = _resolve_schema()
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Kết nối DB và chạy migration trong một transaction."""
    schema = _resolve_schema()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # Mọi DDL của lần chạy này nằm trong schema dataset đích.
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
