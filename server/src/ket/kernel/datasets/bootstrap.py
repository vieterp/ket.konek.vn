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

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Final

from sqlalchemy import Connection, Engine, select, text, update
from sqlalchemy.orm import Session

from ket.kernel.auditing.control_log import CONTROL_AUDIT_TABLE_NAME
from ket.kernel.datasets.models import (
    CONTROL_SCHEMA_VERSION_KEY,
    Dataset,
    SystemMetadata,
    User,
)
from ket.kernel.datasets.naming import CONTROL_SCHEMA
from ket.kernel.datasets.provisioning import assert_dataset_role_administrable
from ket.kernel.errors import SchemaVersionMismatchError
from ket.kernel.persistence.base import ControlBase
from ket.kernel.security.auth_models import SESSION_TABLE_NAME
from ket.kernel.security.dataset_roles import (
    CONTROL_GROUP_ROLE,
    create_dataset_role_statements,
    repair_dataset_privileges_statements,
    revoke_legacy_login_role_grants,
)
from ket.kernel.security.grants import APP_ROLE, grant_append_only, serial_sequence_name

CONTROL_SCHEMA_VERSION = "2"
"""Tăng khi DDL của schema điều khiển đổi — **kèm** một bước nâng cấp tường minh
trong `_UPGRADE_STEPS`. Tăng hằng số mà không viết bước nâng cấp sẽ làm app từ
chối khởi động trên mọi cụm đã cài (`_stamp_version`), đúng như mong muốn.

Lịch sử: `1` = ba bảng nền (lát 2A). `2` = nhật ký điều khiển + phiên đăng nhập
+ cột danh tính (lát 2B-1a, quyết định D1)."""

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
    """Quyền trên bảng điều khiển cho một bên nhận.

    Sinh từ **một** danh sách rồi áp cho từng bên nhận, thay vì hai danh sách
    song song: thêm bảng điều khiển mà chỉ sửa một trong hai danh sách là loại
    lỗi chỉ lộ ra ở đúng một trong hai đường (trước / sau khi chọn dataset), tức
    là loại lỗi lọt qua phần lớn bộ test.
    """
    return (
        # `users`: runtime phải sửa được (đổi mật khẩu, đánh dấu phải đổi mật
        # khẩu, bật/tắt tài khoản, đếm lần đăng nhập sai). Không cho DELETE —
        # xóa người dùng làm mất dấu vết `audit_log.user_id`; vô hiệu hóa
        # (`is_active`) mới là thao tác đúng.
        f"GRANT SELECT, INSERT, UPDATE ON public.{User.__tablename__} TO {grantee}",
        f"GRANT USAGE, SELECT ON SEQUENCE public.{User.__tablename__}_id_seq TO {grantee}",
        # `datasets`: runtime chỉ đọc để định tuyến. Tạo dữ liệu kế toán là thao
        # tác đặc quyền chạy bằng `ket_owner` (FR-SYS-001).
        f"GRANT SELECT ON public.{Dataset.__tablename__} TO {grantee}",
        f"GRANT SELECT ON public.{SystemMetadata.__tablename__} TO {grantee}",
        # `auth_sessions`: cấp phiên, chạm `last_seen_at`, thu hồi. Không DELETE
        # — thu hồi là đánh dấu `revoked_at`, và "phiên này sống từ lúc nào tới
        # lúc nào" phải trả lời được sau sự cố. Dọn phiên hết hạn là việc của
        # một job ở lát sau, chạy bằng `ket_owner`.
        f"GRANT SELECT, INSERT, UPDATE ON public.{SESSION_TABLE_NAME} TO {grantee}",
        f"GRANT USAGE, SELECT ON SEQUENCE public.{SESSION_TABLE_NAME}_id_seq TO {grantee}",
        # `control_audit_log`: chỉ-thêm, cùng khuôn RT-02 với `audit_log` của
        # dataset. Dùng lại `grant_append_only` chứ không viết tay lệnh GRANT —
        # bài học C1: hai đường cấp quyền chép rời nhau thì một đường sẽ trôi.
        *grant_append_only(
            CONTROL_AUDIT_TABLE_NAME,
            grantee=grantee,
            sequence=serial_sequence_name(CONTROL_AUDIT_TABLE_NAME),
            schema=CONTROL_SCHEMA,
        ),
    )


def ensure_database_roles(admin_engine: Engine) -> None:
    """Tạo `ket_owner`/`ket_app` (cần superuser). Chạy lại được.

    `AUTOCOMMIT` vì `CREATE ROLE`/`GRANT ... ON DATABASE` không chạy trong khối
    transaction cùng các lệnh khác một cách đáng tin trên mọi phiên bản PG.
    """
    script = resources.files("ket.kernel.security").joinpath("roles.sql").read_text("utf-8")
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(script)


@dataclass(frozen=True)
class _UpgradeStep:
    """Một bước nâng cấp schema điều khiển, từ phiên bản này sang phiên bản kế."""

    source: str
    target: str
    apply: Callable[[Connection], None]


def _upgrade_1_to_2(connection: Connection) -> None:
    """Lát 2B-1a: cột danh tính mới trên `users` (quyết định D1).

    **Chỉ `ALTER`, không `CREATE TABLE`.** Bảng mới (`auth_sessions`,
    `control_audit_log`) do `create_all` dựng — nó chạy trước và tạo đúng những
    bảng còn thiếu, trên cụm mới lẫn cụm cũ. Lặp lại `CREATE TABLE` ở đây là
    chép định nghĩa bảng lần thứ hai, và bản chép sẽ trôi khỏi model.

    `IF NOT EXISTS` để bước này chạy lại được: nâng cấp bị ngắt giữa chừng
    (mất điện, đóng máy) phải chạy tiếp được mà không cần sửa tay DB.

    Tên bảng viết **thẳng** vào câu lệnh chứ không ghép từ `User.__tablename__`.
    Bước nâng cấp là một mẩu lịch sử đông cứng, giống hệt một migration Alembic:
    nó mô tả DDL cần chạy trên cụm **phiên bản 1**, và nếu ngày mai model đổi tên
    bảng thì câu lệnh này vẫn phải nói `users` — tên của ngày đó. Ghép từ model
    sẽ làm bước nâng cấp lặng lẽ đổi nghĩa theo mã hiện tại.
    """
    for statement in (
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS totp_required BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS totp_enrolled_at TIMESTAMPTZ",
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS totp_last_counter BIGINT",
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ",
    ):
        connection.exec_driver_sql(statement)


_UPGRADE_STEPS: Final[tuple[_UpgradeStep, ...]] = (
    _UpgradeStep(source="1", target="2", apply=_upgrade_1_to_2),
)
"""Chuỗi bước nâng cấp, theo thứ tự. Thêm bước = thêm một phần tử **và** tăng
`CONTROL_SCHEMA_VERSION`."""


def _upgrade_path(found: str) -> tuple[_UpgradeStep, ...]:
    """Chuỗi bước đưa `found` lên phiên bản hiện tại; rỗng nếu không có đường.

    Rỗng bao gồm cả trường hợp DB **mới hơn** mã nguồn (binary cũ gặp DB đã nâng
    cấp). Không tự xử lý ở đây: người gọi để `_stamp_version` báo lỗi, giữ đúng
    một chỗ phát ra thông điệp lệch phiên bản.
    """
    path: list[_UpgradeStep] = []
    version = found
    for step in _UPGRADE_STEPS:
        if step.source == version:
            path.append(step)
            version = step.target
    return tuple(path) if version == CONTROL_SCHEMA_VERSION else ()


def _apply_upgrades(owner_engine: Engine, found: str) -> None:
    """Chạy các bước nâng cấp trong **một** transaction rồi ghi phiên bản mới.

    Một transaction cho cả chuỗi: nâng cấp dở dang là trạng thái không ai kiểm
    được — số phiên bản nói một đằng, cấu trúc bảng nói một nẻo. PostgreSQL cho
    phép DDL trong transaction, nên ở đây không phải đánh đổi gì.
    """
    path = _upgrade_path(found)
    if not path:
        return
    with owner_engine.begin() as connection:
        for step in path:
            step.apply(connection)
        connection.execute(
            update(SystemMetadata)
            .where(SystemMetadata.key == CONTROL_SCHEMA_VERSION_KEY)
            .values(value=CONTROL_SCHEMA_VERSION)
        )


def ensure_control_schema(owner_engine: Engine) -> None:
    """Tạo/nâng cấp bảng điều khiển và cấp quyền cho vai trò runtime.

    Chạy bằng `ket_owner` để bảng thuộc owner — cùng lý do với `audit_log`:
    vai trò runtime không được sửa cấu trúc thứ nó đang dùng.

    Ba bước, theo đúng thứ tự này:

    1. `create_all` — tạo bảng **còn thiếu**. Nó không sửa bảng đã có, nên nó
       giải quyết được "bảng mới" mà không giải quyết được "cột mới".
    2. Bước nâng cấp tường minh — phần `create_all` không làm (xem
       `_upgrade_1_to_2`). Đọc phiên bản **trước** `create_all` vì `create_all`
       không đổi phiên bản nhưng làm mất dấu "cụm này mới hay cũ".
    3. Cấp quyền — cho **mọi** bảng, mọi lần chạy: quyền là thứ `pg_restore`
       mang theo không đầy đủ, và chạy lại thì không tốn gì.
    """
    found = control_schema_version(owner_engine)
    provisioned = _control_schema_exists(owner_engine)

    # "Chưa có phiên bản" có hai nghĩa rất khác nhau, và gộp chúng lại là một
    # đường dán nhãn khống: cụm **trống** thì đúng là mới (đi tiếp, `_stamp_version`
    # ghi nhãn đầu tiên), nhưng cụm **đã có bảng** mà mất dòng phiên bản — dump
    # thiếu, ai đó xóa nhầm một dòng `system_metadata` — sẽ được `create_all` bỏ
    # qua (bảng đã tồn tại), bỏ qua luôn bước nâng cấp, rồi nhận nhãn phiên bản
    # hiện tại. Kết quả: DB nói "phiên bản 2" trong khi `users` còn thiếu năm
    # cột, và triệu chứng đầu tiên là `UndefinedColumn` giữa luồng đăng nhập.
    if provisioned and found is None:
        raise SchemaVersionMismatchError(
            "Schema điều khiển đã dựng nhưng không có dòng phiên bản trong system_metadata. "
            "Không thể suy ra cấu trúc hiện tại nên cũng không thể nâng cấp an toàn — "
            "khôi phục lại bản sao lưu gần nhất, hoặc ghi đúng phiên bản đang có bằng tay.",
            expected=CONTROL_SCHEMA_VERSION,
        )

    ControlBase.metadata.create_all(owner_engine)
    if found is not None and found != CONTROL_SCHEMA_VERSION:
        _apply_upgrades(owner_engine, found)
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
            present = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :schema"
                    ),
                    {"schema": schema},
                ).all()
            }
            for statement in repair_dataset_privileges_statements(schema, present_tables=present):
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


def _control_schema_exists(engine: Engine) -> bool:
    """Cụm đã dựng schema điều khiển chưa (không quan tâm phiên bản nào).

    Đo bằng `public.users` chứ bằng `system_metadata`: `system_metadata` chính là
    bảng mang dòng phiên bản, nên dùng nó để trả lời "đã dựng chưa" sẽ không phân
    biệt được "cụm trống" với "cụm đủ bảng nhưng mất dòng phiên bản" — đúng hai
    trường hợp cần phân biệt (xem `ensure_control_schema`).
    """
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT to_regclass(:name)"),
            {"name": f"{CONTROL_SCHEMA}.{User.__tablename__}"},
        ).scalar_one_or_none()
    return found is not None


def control_schema_version(engine: Engine) -> str | None:
    """Phiên bản schema điều khiển đang có trong DB.

    `None` = **không đọc được phiên bản**, gồm cả cụm chưa dựng lẫn cụm đã dựng
    mà mất dòng phiên bản. Người gọi phải tự phân biệt hai ca đó bằng
    `_control_schema_exists` — chúng dẫn tới hai hành động ngược nhau.
    """
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
