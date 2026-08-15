"""Dựng cụm: hai vai trò DB + schema điều khiển. Chạy trước dataset đầu tiên.

Vì sao schema điều khiển **không** do Alembic quản lý, trong khi schema dataset
thì có: Alembic ở đây chạy **lặp cho từng dataset** với `version_table_schema`
riêng. Để nó quản thêm `public` sẽ cần nhánh migration thứ hai với bảng phiên
bản riêng — thêm hẳn một chiều phức tạp cho ba bảng gần như không đổi. Đổi lại,
schema điều khiển dùng DDL idempotent + số phiên bản ghi trong
`system_metadata`, và **có kiểm** lúc khởi động y như dataset.

Đánh đổi đã cân nhắc: nếu ba bảng này bắt đầu đổi thường xuyên (khả năng cao
nhất là khi thêm SSO hoặc chính sách mật khẩu), hãy chuyển sang nhánh Alembic
thứ hai chứ đừng chồng thêm bước tay ở đây.
"""

from __future__ import annotations

from importlib import resources

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from konek.kernel.datasets.models import (
    CONTROL_SCHEMA_VERSION_KEY,
    Dataset,
    SystemMetadata,
    User,
)
from konek.kernel.errors import SchemaVersionMismatchError
from konek.kernel.persistence.base import ControlBase
from konek.kernel.security.grants import APP_ROLE

CONTROL_SCHEMA_VERSION = "1"
"""Tăng khi DDL của schema điều khiển đổi — **kèm** một bước nâng cấp tường minh.
Tăng hằng số mà không viết bước nâng cấp sẽ làm app từ chối khởi động trên mọi
cụm đã cài (`_stamp_version`), đúng như mong muốn."""

_CONTROL_TABLE_GRANTS: tuple[str, ...] = (
    # `users`: runtime phải sửa được (đổi mật khẩu, đánh dấu phải đổi mật khẩu,
    # bật/tắt tài khoản). Không cho DELETE — xóa người dùng làm mất dấu vết
    # `audit_log.user_id`; vô hiệu hóa (`is_active`) mới là thao tác đúng.
    f"GRANT SELECT, INSERT, UPDATE ON public.{User.__tablename__} TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON SEQUENCE public.{User.__tablename__}_id_seq TO {APP_ROLE}",
    # `datasets`: runtime chỉ đọc để định tuyến. Tạo dữ liệu kế toán là thao tác
    # đặc quyền chạy bằng `konek_owner` (FR-SYS-001).
    f"GRANT SELECT ON public.{Dataset.__tablename__} TO {APP_ROLE}",
    f"GRANT SELECT ON public.{SystemMetadata.__tablename__} TO {APP_ROLE}",
)


def ensure_database_roles(admin_engine: Engine) -> None:
    """Tạo `konek_owner`/`konek_app` (cần superuser). Chạy lại được.

    `AUTOCOMMIT` vì `CREATE ROLE`/`GRANT ... ON DATABASE` không chạy trong khối
    transaction cùng các lệnh khác một cách đáng tin trên mọi phiên bản PG.
    """
    script = resources.files("konek.kernel.security").joinpath("roles.sql").read_text("utf-8")
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(script)


def ensure_control_schema(owner_engine: Engine) -> None:
    """Tạo/cập nhật ba bảng điều khiển và cấp quyền cho vai trò runtime.

    Chạy bằng `konek_owner` để bảng thuộc owner — cùng lý do với `audit_log`:
    vai trò runtime không được sửa cấu trúc thứ nó đang dùng.
    """
    ControlBase.metadata.create_all(owner_engine)
    with owner_engine.begin() as connection:
        for statement in _CONTROL_TABLE_GRANTS:
            connection.exec_driver_sql(statement)
    _stamp_version(owner_engine)


def _stamp_version(owner_engine: Engine) -> None:
    """Ghi phiên bản cho cụm mới; **từ chối** nếu cụm cũ hơn mã nguồn.

    Trước đây hàm này lặng lẽ ghi đè phiên bản cũ bằng hằng số hiện tại. Kết
    quả là `verify_control_schema` không bao giờ hỏng được — một DB v1 gặp
    binary v2 sẽ được **dán nhãn** v2 trong khi `create_all` không hề thêm cột
    mới (nó chỉ tạo bảng còn thiếu, không sửa bảng đã có). Đó đúng là kiểu hỏng
    âm thầm mà số phiên bản sinh ra để chống.

    Nâng cấp schema điều khiển phải là một bước **tường minh**: viết hàm nâng
    cấp, chạy, rồi mới ghi phiên bản mới.
    """
    with Session(owner_engine) as session, session.begin():
        current = session.get(SystemMetadata, CONTROL_SCHEMA_VERSION_KEY)
        if current is None:
            session.add(
                SystemMetadata(key=CONTROL_SCHEMA_VERSION_KEY, value=CONTROL_SCHEMA_VERSION)
            )
            return
        if current.value != CONTROL_SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                "Schema điều khiển trong DB khác phiên bản mã nguồn — cần bước nâng cấp "
                "tường minh, không tự dán nhãn lại",
                expected=CONTROL_SCHEMA_VERSION,
                found=current.value,
            )


def control_schema_version(engine: Engine) -> str | None:
    """Phiên bản schema điều khiển đang có trong DB (None = chưa dựng)."""
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT to_regclass(:name)"),
            {"name": f"public.{SystemMetadata.__tablename__}"},
        ).scalar_one_or_none()
        if exists is None:
            return None
        return connection.execute(
            select(SystemMetadata.value).where(SystemMetadata.key == CONTROL_SCHEMA_VERSION_KEY)
        ).scalar_one_or_none()


def verify_control_schema(engine: Engine) -> None:
    """Chặn khởi động nếu schema điều khiển chưa dựng hoặc lệch phiên bản (LD-05)."""
    found = control_schema_version(engine)
    if found is None:
        raise SchemaVersionMismatchError(
            "Chưa dựng schema điều khiển — chạy bước khởi tạo cụm trước khi khởi động app server",
            expected=CONTROL_SCHEMA_VERSION,
        )
    if found != CONTROL_SCHEMA_VERSION:
        raise SchemaVersionMismatchError(
            "Schema điều khiển lệch phiên bản với mã nguồn đang chạy",
            expected=CONTROL_SCHEMA_VERSION,
            found=found,
        )
