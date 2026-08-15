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

DATASET_ROLE_SUFFIX: Final[str] = "_app"
"""Hậu tố tên vai trò DB của một dataset: schema `ds_alpha` → vai trò
`ds_alpha_app` (ADR-017 §Consequences, quyết định D3 của phase 2).

Vai trò suy từ **tên schema** chứ không từ mã dataset: schema là thứ đã đi qua
`validate_schema_name` và cũng là thứ migration nhìn thấy, nên hai bên không thể
lệch nhau."""

MAX_IDENTIFIER_LENGTH: Final[int] = 63
"""Trần identifier của PostgreSQL. Vượt quá thì PG **cắt bớt âm thầm** — hai
dataset khác mã có thể trỏ về cùng một schema."""

MAX_DATASET_CODE_LENGTH: Final[int] = (
    MAX_IDENTIFIER_LENGTH - len(DATASET_SCHEMA_PREFIX) - len(DATASET_ROLE_SUFFIX)
)
"""Trần mã dataset = 56 ký tự.

Trừ **cả hai** phần nối thêm, không chỉ tiền tố schema: mã dài 60 ký tự vẫn cho
ra tên schema hợp lệ (63) nhưng tên vai trò 67 ký tự thì PostgreSQL cắt còn 63.
Hai dataset chỉ khác nhau ở bốn ký tự cuối sẽ dùng **chung một vai trò** — tức
là mất đúng cơ chế cô lập mà vai trò per-dataset sinh ra để dựng, mà không có
thông báo lỗi nào."""

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


def validate_grantable_schema(schema: str) -> str:
    """Tên schema dùng được trong lệnh **cấp quyền**: dataset hoặc điều khiển.

    Khác `validate_schema_name` đúng một điểm: chấp nhận `public`. Cấp quyền là
    việc duy nhất chạm cả hai loại schema (`grants.py` phục vụ cả bảng dataset
    lẫn `control_audit_log`), trong khi mọi đường khác — `CREATE SCHEMA`,
    `SET search_path`, định tuyến request — vẫn **phải** từ chối `public`, nếu
    không một dataset tên `public` sẽ ghi đè lên schema điều khiển.

    Tách thành hàm riêng thay vì thêm cờ vào `validate_schema_name`: một cờ
    `allow_control=True` là thứ sẽ bị sao chép sang chỗ khác vì "chạy được", và
    chỗ đó có thể là đường định tuyến.
    """
    if schema == CONTROL_SCHEMA:
        return CONTROL_SCHEMA
    return validate_schema_name(schema)


def validate_dataset_code(code: str) -> str:
    """Kiểm mã dữ liệu kế toán do người dùng đặt (FR-SYS-001)."""
    if not _DATASET_CODE_PATTERN.match(code):
        raise InvalidSchemaNameError(
            "Mã dữ liệu kế toán chỉ được gồm chữ thường, chữ số và gạch dưới, "
            "bắt đầu bằng chữ hoặc số",
            code=code,
        )
    if len(code) > MAX_DATASET_CODE_LENGTH:
        raise InvalidSchemaNameError(
            f"Mã dữ liệu kế toán dài quá {MAX_DATASET_CODE_LENGTH} ký tự",
            code=code,
            length=len(code),
        )
    return code


def schema_name_for(code: str) -> str:
    """Mã dữ liệu kế toán → tên schema (`kt2026` → `ds_kt2026`)."""
    return validate_schema_name(f"{DATASET_SCHEMA_PREFIX}{validate_dataset_code(code)}")


def role_name_for_schema(schema: str) -> str:
    """Tên schema → tên vai trò DB của dataset đó (`ds_kt2026` → `ds_kt2026_app`).

    Kiểm lại độ dài tại đây chứ không tin vào `validate_dataset_code`: hàm này
    cũng nhận schema đến từ bảng đăng ký và từ `migrations/env.py`, những đường
    không đi qua bước kiểm mã. Cắt tên âm thầm ở đây nghĩa là hai dataset dùng
    chung một vai trò.
    """
    role = f"{validate_schema_name(schema)}{DATASET_ROLE_SUFFIX}"
    if len(role) > MAX_IDENTIFIER_LENGTH:
        raise InvalidSchemaNameError(
            f"Tên vai trò dataset dài quá {MAX_IDENTIFIER_LENGTH} ký tự",
            schema=schema,
            length=len(role),
        )
    return role
