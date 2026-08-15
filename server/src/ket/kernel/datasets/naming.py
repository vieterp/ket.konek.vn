"""Tên schema của một dữ liệu kế toán — ranh giới tin cậy duy nhất (ADR-017).

Một dữ liệu kế toán = một PostgreSQL schema. Tên schema **không tham số hóa
được** trong SQL (nó là identifier, không phải giá trị), nên mọi chỗ ghép tên
schema vào câu lệnh đều phải đi qua `validate_schema_name` trước. Đây là ngoại
lệ duy nhất của quy tắc "không nối chuỗi SQL" và nó được rào bằng whitelist ký
tự, không phải bằng escape.

Module này cố ý **không phụ thuộc gì** (kể cả SQLAlchemy) để `migrations/env.py`
dùng lại được cùng một bộ luật — hai bộ luật khác nhau cho cùng một tên schema
là cách chắc chắn nhất để migration chạy nhầm chỗ.
"""

from __future__ import annotations

import re
from typing import Final

from ket.kernel.errors import InvalidSchemaNameError

CONTROL_SCHEMA: Final[str] = "public"
"""Schema điều khiển: `datasets`, `users` toàn cục, `system_metadata`."""

DATASET_SCHEMA_PREFIX: Final[str] = "ds_"

MAX_IDENTIFIER_LENGTH: Final[int] = 63
"""Trần identifier của PostgreSQL. Vượt quá thì PG **cắt bớt âm thầm** — hai
dataset khác mã có thể trỏ về cùng một schema."""

_SCHEMA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]*$")

_DATASET_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_]*$")

RESERVED_SCHEMA_NAMES: Final[frozenset[str]] = frozenset(
    {CONTROL_SCHEMA, "pg_catalog", "information_schema", "pg_toast", "pg_temp"}
)


def validate_schema_name(schema: str) -> str:
    """Trả lại `schema` nếu dùng được làm identifier schema dataset, không thì ném lỗi."""
    if not _SCHEMA_PATTERN.match(schema):
        raise InvalidSchemaNameError(
            "Tên schema chỉ được gồm chữ thường, chữ số, gạch dưới và không bắt đầu bằng chữ số",
            schema=schema,
        )
    if len(schema) > MAX_IDENTIFIER_LENGTH:
        raise InvalidSchemaNameError(
            f"Tên schema dài quá {MAX_IDENTIFIER_LENGTH} ký tự",
            schema=schema,
            length=len(schema),
        )
    if schema in RESERVED_SCHEMA_NAMES or schema.startswith("pg_"):
        raise InvalidSchemaNameError("Tên schema trùng schema hệ thống", schema=schema)
    return schema


def validate_dataset_code(code: str) -> str:
    """Kiểm mã dữ liệu kế toán do người dùng đặt (FR-SYS-001)."""
    if not _DATASET_CODE_PATTERN.match(code):
        raise InvalidSchemaNameError(
            "Mã dữ liệu kế toán chỉ được gồm chữ thường, chữ số và gạch dưới, "
            "bắt đầu bằng chữ hoặc số",
            code=code,
        )
    if len(code) > MAX_IDENTIFIER_LENGTH - len(DATASET_SCHEMA_PREFIX):
        raise InvalidSchemaNameError(
            f"Mã dữ liệu kế toán dài quá "
            f"{MAX_IDENTIFIER_LENGTH - len(DATASET_SCHEMA_PREFIX)} ký tự",
            code=code,
            length=len(code),
        )
    return code


def schema_name_for(code: str) -> str:
    """Mã dữ liệu kế toán → tên schema (`kt2026` → `ds_kt2026`)."""
    return validate_schema_name(f"{DATASET_SCHEMA_PREFIX}{validate_dataset_code(code)}")
