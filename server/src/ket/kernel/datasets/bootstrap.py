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

from ket.kernel.datasets.models import (
    CONTROL_SCHEMA_VERSION_KEY,
    Dataset,
    SystemMetadata,
    User,
)
from ket.kernel.datasets.provisioning import assert_dataset_role_administrable
from ket.kernel.errors import SchemaVersionMismatchError
from ket.kernel.persistence.base import ControlBase
from ket.kernel.security.dataset_roles import (
    CONTROL_GROUP_ROLE,
    create_dataset_role_statements,
    repair_dataset_privileges_statements,
    revoke_legacy_login_role_grants,
)
from ket.kernel.security.grants import APP_ROLE

CONTROL_SCHEMA_VERSION = "1"
"""Tăng khi DDL của schema điều khiển đổi — **kèm** một bước nâng cấp tường minh.
Tăng hằng số mà không viết bước nâng cấp sẽ làm app từ chối khởi động trên mọi
cụm đã cài (`_stamp_version`), đúng như mong muốn."""

CONTROL_GRANTEES: tuple[str, ...] = (APP_ROLE, CONTROL_GROUP_ROLE)
"""Hai bên nhận quyền trên bảng điều khiển, và lý do phải có cả hai (D3):

* `ket_app` — đường **trước khi chọn dataset**: đăng nhập, liệt kê dữ liệu kế
  toán. Lúc đó chưa `SET ROLE` được vì chưa biết chọn vai trò nào.
* `ket_control` — đường **sau khi `SET ROLE ds_<mã>_app`**: `current_user` đã
  đổi, nên quyền cấp thẳng cho `ket_app` không còn áp dụng; vai trò dataset lấy
  quyền này qua kế thừa nhóm.

`ket_app` là `NOINHERIT` nên nó **không** lấy được quyền qua đường nhóm — hai
lần cấp không thừa nhau.
"""


def control_table_grants(grantee: str) -> tuple[str, ...]:
    """Quyền trên ba bảng điều khiển cho một bên nhận.

    Sinh từ **một** danh sách rồi áp cho từng bên nhận, thay vì hai danh sách
    song song: thêm bảng điều khiển mà chỉ sửa một trong hai danh sách là loại
    lỗi chỉ lộ ra ở đúng một trong hai đường (trước / sau khi chọn dataset), tức
    là loại lỗi lọt qua phần lớn bộ test.
    """
    return (
        # `users`: runtime phải sửa được (đổi mật khẩu, đánh dấu phải đổi mật
        # khẩu, bật/tắt tài khoản). Không cho DELETE — xóa người dùng làm mất
        # dấu vết `audit_log.user_id`; vô hiệu hóa (`is_active`) mới là thao tác
        # đúng.
        f"GRANT SELECT, INSERT, UPDATE ON public.{User.__tablename__} TO {grantee}",
        f"GRANT USAGE, SELECT ON SEQUENCE public.{User.__tablename__}_id_seq TO {grantee}",
        # `datasets`: runtime chỉ đọc để định tuyến. Tạo dữ liệu kế toán là thao
        # tác đặc quyền chạy bằng `ket_owner` (FR-SYS-001).
        f"GRANT SELECT ON public.{Dataset.__tablename__} TO {grantee}",
        f"GRANT SELECT ON public.{SystemMetadata.__tablename__} TO {grantee}",
    )


def ensure_database_roles(admin_engine: Engine) -> None:
    """Tạo `ket_owner`/`ket_app` (cần superuser). Chạy lại được.

    `AUTOCOMMIT` vì `CREATE ROLE`/`GRANT ... ON DATABASE` không chạy trong khối
    transaction cùng các lệnh khác một cách đáng tin trên mọi phiên bản PG.
    """
    script = resources.files("ket.kernel.security").joinpath("roles.sql").read_text("utf-8")
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(script)


def ensure_control_schema(owner_engine: Engine) -> None:
    """Tạo/cập nhật ba bảng điều khiển và cấp quyền cho vai trò runtime.

    Chạy bằng `ket_owner` để bảng thuộc owner — cùng lý do với `audit_log`:
    vai trò runtime không được sửa cấu trúc thứ nó đang dùng.
    """
    ControlBase.metadata.create_all(owner_engine)
    with owner_engine.begin() as connection:
        for grantee in CONTROL_GRANTEES:
            for statement in control_table_grants(grantee):
                connection.exec_driver_sql(statement)
    _stamp_version(owner_engine)


def ensure_dataset_roles(owner_engine: Engine) -> tuple[str, ...]:
    """Dựng lại vai trò DB cho **mọi dataset đã đăng ký**. Trả về các schema đã xử lý.

    Vai trò là đối tượng **cấp cụm**, nên `pg_dump` của một database không chứa
    chúng. Từ D3, mỗi dataset cần một vai trò riêng mà trước đây chỉ
    `provision_dataset` tạo ra — và hàm đó từ chối chạy lại với mã đã đăng ký.
    Hệ quả trước khi có hàm này: khôi phục bản sao lưu sang cụm mới cho ra một
    database đầy đủ sổ sách mà app **không khởi động được**
    (`role "ds_x_app" does not exist`), không đường nào sửa ngoài SQL tay.

    Vì `pg_dump` theo từng database là quy trình sao lưu đã chốt, hàm này là nửa
    còn lại của quy trình khôi phục:

    ```
    psql -f roles.sql        # vai trò nền, chạy bằng superuser
    pg_restore …             # dữ liệu
    ensure_cluster(...)      # bảng điều khiển + vai trò của từng dataset
    ```

    Idempotent: chạy trên cụm đã đúng thì không đổi gì.
    """
    with Session(owner_engine) as session:
        schemas = tuple(session.scalars(select(Dataset.schema_name)).all())

    with owner_engine.begin() as connection:
        for schema in schemas:
            assert_dataset_role_administrable(connection, schema)
            for statement in create_dataset_role_statements(schema):
                connection.exec_driver_sql(statement)
            # Vai trò mới dựng lại không mang theo quyền nào: `DROP ROLE` đã xóa
            # sạch mọi mục ACL trỏ tới nó. Dựng vai trò mà không cấp lại quyền là
            # sửa được đúng một nửa — app vẫn chết, chỉ đổi thông điệp từ
            # "role does not exist" thành "permission denied for table".
            for statement in repair_dataset_privileges_statements(schema):
                connection.exec_driver_sql(statement)
            # Chỉ chạy được ở đây chứ không ở `provision_dataset`: lúc provision,
            # schema vừa tạo còn rỗng nên `ON ALL TABLES` không có gì để thu hồi.
            # Đây mới là nơi gặp schema **đã có bảng** — gồm cả bảng do bản cài
            # trước D3 cấp quyền thẳng cho `ket_app`.
            for statement in revoke_legacy_login_role_grants(schema):
                connection.exec_driver_sql(statement)

    return schemas


def ensure_cluster(owner_engine: Engine) -> None:
    """Bước khởi tạo/nâng cấp/khôi phục cụm, chạy bằng `ket_owner`.

    Một điểm vào duy nhất để không ai phải nhớ thứ tự: bảng điều khiển trước
    (vai trò dataset đọc `public.datasets` để biết cần dựng những gì), vai trò
    dataset sau. Vai trò **nền** (`ket_owner`/`ket_app`/`ket_control`) vẫn phải
    dựng trước bằng superuser — xem `ensure_database_roles`.
    """
    ensure_control_schema(owner_engine)
    ensure_dataset_roles(owner_engine)


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
