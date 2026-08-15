"""Cấp quyền bảng cho vai trò runtime `konek_app` (RT-02).

Mọi migration tạo bảng **phải** gọi một trong hai hàm dưới đây cho bảng đó.
Không có `GRANT ... ON ALL TABLES` quét cả schema ở cuối migration: lệnh đó
chỉ áp cho bảng đang tồn tại lúc chạy, nên bảng của migration sau sẽ **âm thầm
không có quyền** và lỗi chỉ lộ ra ở chỗ khác, muộn hơn nhiều. Cấp quyền ngay
cạnh `create_table` giữ hai thứ đó không bao giờ lệch nhau.

Nguyên tắc phân loại:

* `grant_app_read_write` — bảng nghiệp vụ bình thường.
* `grant_app_append_only` — bảng **chỉ được thêm**, không sửa/xóa: `audit_log`
  (FR-NFR-012/013) và mọi bảng bất biến sau này (`gl_postings` ở phase 4 dùng
  cùng cơ chế). Chủ sở hữu bảng phải là `konek_owner`, nếu không `REVOKE` vô
  tác dụng — chủ sở hữu tự bỏ qua REVOKE của chính mình.
"""

from __future__ import annotations

from typing import Final

from konek.kernel.security.rls import validate_identifier

APP_ROLE: Final[str] = "konek_app"
OWNER_ROLE: Final[str] = "konek_owner"


def grant_app_read_write(table: str, *, sequence: str | None = None) -> tuple[str, ...]:
    """Quyền đầy đủ trên bảng nghiệp vụ cho vai trò runtime."""
    validate_identifier(table)
    statements = [
        f"ALTER TABLE {table} OWNER TO {OWNER_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}",
    ]
    statements.extend(_sequence_grants(table, sequence))
    return tuple(statements)


def grant_app_append_only(table: str, *, sequence: str | None = None) -> tuple[str, ...]:
    """Chỉ `INSERT`/`SELECT` — vai trò runtime không sửa/xóa/cắt bảng được.

    `REVOKE ALL` chạy trước `GRANT` để quyền không cộng dồn từ lần chạy trước
    hay từ `GRANT ... TO PUBLIC` của ai đó.
    """
    validate_identifier(table)
    statements = [
        f"ALTER TABLE {table} OWNER TO {OWNER_ROLE}",
        f"REVOKE ALL ON {table} FROM {APP_ROLE}",
        f"REVOKE ALL ON {table} FROM PUBLIC",
        f"GRANT INSERT, SELECT ON {table} TO {APP_ROLE}",
    ]
    statements.extend(_sequence_grants(table, sequence))
    return tuple(statements)


def _sequence_grants(table: str, sequence: str | None) -> tuple[str, ...]:
    """Quyền trên sequence của khóa tự tăng.

    Thiếu lệnh này thì `INSERT` fail với "permission denied for sequence" —
    một lỗi rất dễ mất hàng giờ vì thông điệp không nhắc gì tới bảng.
    """
    if sequence is None:
        return ()
    validate_identifier(sequence)
    return (
        f"ALTER SEQUENCE {sequence} OWNER TO {OWNER_ROLE}",
        f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {APP_ROLE}",
    )


def serial_sequence_name(table: str, column: str = "id") -> str:
    """Tên sequence PostgreSQL tự đặt cho cột `SERIAL`/`BIGSERIAL`."""
    return f"{table}_{column}_seq"
