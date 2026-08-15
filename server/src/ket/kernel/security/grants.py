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

from ket.kernel.security.rls import validate_identifier

APP_ROLE: Final[str] = "ket_app"
"""Vai trò **đăng nhập** của app server. Từ D3 nó chỉ còn giữ quyền trên bảng
schema điều khiển; quyền trên bảng dataset thuộc về `ds_<mã>_app`."""

OWNER_ROLE: Final[str] = "ket_owner"


def grant_read_write(table: str, *, grantee: str, sequence: str | None = None) -> tuple[str, ...]:
    """Quyền đầy đủ trên bảng nghiệp vụ cho vai trò runtime của dataset."""
    validate_identifier(table)
    validate_identifier(grantee)
    statements = [
        f"ALTER TABLE {table} OWNER TO {OWNER_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {grantee}",
    ]
    statements.extend(_sequence_grants(table, sequence, grantee))
    return tuple(statements)


def grant_append_only(table: str, *, grantee: str, sequence: str | None = None) -> tuple[str, ...]:
    """Chỉ `INSERT`/`SELECT` — vai trò runtime không sửa/xóa/cắt bảng được.

    Thứ giữ tính bất biến là `REVOKE ALL … FROM PUBLIC` cộng với việc chỉ cấp
    `INSERT, SELECT`. `REVOKE ALL … FROM {grantee}` đứng trước là phòng thủ dư:
    migration chạy đúng một lần cho mỗi schema nên trên bảng vừa tạo không có gì
    để thu hồi, nhưng cùng bộ câu lệnh này còn được dùng lại ở đường sửa chữa
    (`dataset_roles.repair_dataset_privileges_statements`), nơi bảng **đã có**
    quyền cũ và thứ tự revoke-rồi-grant là thứ duy nhất cho ra kết quả đúng.
    """
    validate_identifier(table)
    validate_identifier(grantee)
    statements = [
        f"ALTER TABLE {table} OWNER TO {OWNER_ROLE}",
        f"REVOKE ALL ON {table} FROM {grantee}",
        f"REVOKE ALL ON {table} FROM PUBLIC",
        f"GRANT INSERT, SELECT ON {table} TO {grantee}",
    ]
    statements.extend(_sequence_grants(table, sequence, grantee))
    return tuple(statements)


def _sequence_grants(table: str, sequence: str | None, grantee: str) -> tuple[str, ...]:
    """Quyền trên sequence của khóa tự tăng.

    Thiếu lệnh này thì `INSERT` fail với "permission denied for sequence" —
    một lỗi rất dễ mất hàng giờ vì thông điệp không nhắc gì tới bảng.
    """
    if sequence is None:
        return ()
    validate_identifier(sequence)
    return (
        f"ALTER SEQUENCE {sequence} OWNER TO {OWNER_ROLE}",
        f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {grantee}",
    )


def serial_sequence_name(table: str, column: str = "id") -> str:
    """Tên sequence PostgreSQL tự đặt cho cột `SERIAL`/`BIGSERIAL`."""
    return f"{table}_{column}_seq"
