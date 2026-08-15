"""Cấp quyền bảng cho vai trò runtime (RT-02, D3).

Mọi migration tạo bảng **phải** gọi một trong hai hàm dưới đây cho bảng đó.
Không có `GRANT ... ON ALL TABLES` quét cả schema ở cuối migration: lệnh đó
chỉ áp cho bảng đang tồn tại lúc chạy, nên bảng của migration sau sẽ **âm thầm
không có quyền** và lỗi chỉ lộ ra ở chỗ khác, muộn hơn nhiều. Cấp quyền ngay
cạnh `create_table` giữ hai thứ đó không bao giờ lệch nhau.

Nguyên tắc phân loại:

* `grant_read_write` — bảng nghiệp vụ bình thường.
* `grant_append_only` — bảng **chỉ được thêm**, không sửa/xóa: `audit_log`
  (FR-NFR-012/013) và mọi bảng bất biến sau này (`gl_postings` ở phase 4 dùng
  cùng cơ chế). Chủ sở hữu bảng phải là `ket_owner`, nếu không `REVOKE` vô
  tác dụng — chủ sở hữu tự bỏ qua REVOKE của chính mình.

**Bên nhận quyền là tham số, không phải hằng số (D3).** Bảng trong schema
dataset cấp cho vai trò riêng của dataset đó (`ds_<mã>_app`), không cấp cho
`ket_app`; đó là thứ khiến hai doanh nghiệp không với được sang nhau ngay cả khi
câu truy vấn ghi rõ tên schema. Migration suy tên vai trò từ schema đang chạy —
xem `_dataset_grantee()` trong `migrations/versions/0001_core_platform.py`.
"""

from __future__ import annotations

from typing import Final

from ket.kernel.auditing.models import AUDIT_TABLE_NAME
from ket.kernel.datasets.naming import validate_grantable_schema
from ket.kernel.security.rls import validate_identifier

APPEND_ONLY_TABLES: Final[frozenset[str]] = frozenset({AUDIT_TABLE_NAME})
"""Bảng **chỉ được thêm** trong schema dataset — nguồn sự thật DUY NHẤT.

Hai đường cấp quyền phải đọc cùng danh sách này: migration (cấp lúc tạo bảng) và
đường sửa chữa (`dataset_roles.repair_dataset_privileges_statements`, cấp lại cho
schema đã có dữ liệu). Trước đây danh sách bị chép làm hai, và hậu quả đo được:
một bảng chỉ-thêm thêm ở phase sau bị `ensure_cluster` **âm thầm nâng lên
read-write** — tức nhật ký bất biến (FR-NFR-012/013) bị chính đường sửa chữa vô
hiệu hóa.

Phase 4 thêm `gl_postings` vào đây cùng lúc với việc tạo bảng."""

READ_ONLY_TABLES: Final[frozenset[str]] = frozenset({"alembic_version"})
"""Bảng vai trò runtime chỉ được **đọc**.

`alembic_version` do chính Alembic tạo nên không migration nào cấp quyền cho nó,
nhưng runtime phải đọc được (kiểm phiên bản lúc khởi động, handshake với client).
Ghi thì là việc của `ket_owner` — vai trò runtime ghi được số phiên bản schema là
một đường làm hỏng dữ liệu rất khó truy."""

APP_ROLE: Final[str] = "ket_app"
"""Vai trò **đăng nhập** của app server. Từ D3 nó chỉ còn giữ quyền trên bảng
schema điều khiển; quyền trên bảng dataset thuộc về `ds_<mã>_app`."""

OWNER_ROLE: Final[str] = "ket_owner"


def _qualify(name: str, schema: str | None) -> str:
    """Tên đối tượng dùng trong DDL, nêu schema khi cần.

    Migration chạy với `search_path` đã trỏ đúng schema nên dùng tên trần; đường
    sửa chữa chạy ngoài request nên phải nêu schema. Cùng một bộ hàm phục vụ cả
    hai — tách đôi là cách hai đường trôi lệch, đúng lỗi mà `APPEND_ONLY_TABLES`
    vừa được dựng để chống.

    `validate_grantable_schema` chứ không `validate_schema_name`: bảng chỉ-thêm
    của schema điều khiển (`public.control_audit_log`, quyết định D1) đi qua đúng
    hàm `grant_append_only` này, nên `public` phải hợp lệ **ở đây** — và chỉ ở
    đây.
    """
    validate_identifier(name)
    if schema is None:
        return name
    return f'"{validate_grantable_schema(schema)}".{name}'


def grant_read_write(
    table: str, *, grantee: str, sequence: str | None = None, schema: str | None = None
) -> tuple[str, ...]:
    """Quyền đầy đủ trên bảng nghiệp vụ cho vai trò runtime của dataset."""
    qualified = _qualify(table, schema)
    validate_identifier(grantee)
    statements = [
        f"ALTER TABLE {qualified} OWNER TO {OWNER_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO {grantee}",
    ]
    statements.extend(_sequence_grants(sequence, grantee, schema))
    return tuple(statements)


def grant_read_only(table: str, *, grantee: str, schema: str | None = None) -> tuple[str, ...]:
    """Chỉ `SELECT` — xem `READ_ONLY_TABLES`.

    `REVOKE ALL` trước `GRANT` vì hàm này chỉ dùng ở đường sửa chữa, nơi bảng đã
    mang quyền cũ cần hạ xuống.
    """
    qualified = _qualify(table, schema)
    validate_identifier(grantee)
    return (
        f"REVOKE ALL ON {qualified} FROM {grantee}",
        f"GRANT SELECT ON {qualified} TO {grantee}",
    )


def grant_append_only(
    table: str, *, grantee: str, sequence: str | None = None, schema: str | None = None
) -> tuple[str, ...]:
    """Chỉ `INSERT`/`SELECT` — vai trò runtime không sửa/xóa/cắt bảng được.

    Thứ giữ tính bất biến là `REVOKE ALL … FROM PUBLIC` cộng với việc chỉ cấp
    `INSERT, SELECT`. `REVOKE ALL … FROM {grantee}` đứng trước là bắt buộc cho
    đường sửa chữa (`dataset_roles.repair_dataset_privileges_statements` gọi
    chính hàm này trên schema đã có dữ liệu, nơi bảng **đã** mang quyền cũ);
    trên bảng vừa tạo trong migration thì nó là lệnh rỗng.
    """
    qualified = _qualify(table, schema)
    validate_identifier(grantee)
    statements = [
        f"ALTER TABLE {qualified} OWNER TO {OWNER_ROLE}",
        f"REVOKE ALL ON {qualified} FROM {grantee}",
        f"REVOKE ALL ON {qualified} FROM PUBLIC",
        f"GRANT INSERT, SELECT ON {qualified} TO {grantee}",
    ]
    statements.extend(_sequence_grants(sequence, grantee, schema))
    return tuple(statements)


def _sequence_grants(sequence: str | None, grantee: str, schema: str | None) -> tuple[str, ...]:
    """Quyền trên sequence của khóa tự tăng.

    Thiếu lệnh này thì `INSERT` fail với "permission denied for sequence" —
    một lỗi rất dễ mất hàng giờ vì thông điệp không nhắc gì tới bảng.
    """
    if sequence is None:
        return ()
    qualified = _qualify(sequence, schema)
    return (
        f"ALTER SEQUENCE {qualified} OWNER TO {OWNER_ROLE}",
        f"GRANT USAGE, SELECT ON SEQUENCE {qualified} TO {grantee}",
    )


def serial_sequence_name(table: str, column: str = "id") -> str:
    """Tên sequence PostgreSQL tự đặt cho cột `SERIAL`/`BIGSERIAL`."""
    return f"{table}_{column}_seq"
