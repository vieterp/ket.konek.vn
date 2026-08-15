"""Nhật ký điều khiển bất biến — cùng khuôn RT-02 với `audit_log` (quyết định D1).

`audit_log` của dataset đã có bộ test riêng. Bảng này cần bộ test **riêng** chứ
không phải "chắc cũng thế": nó nằm ở schema khác (`public`), được cấp quyền bởi
đường khác (`bootstrap.control_table_grants`, không phải migration), và chính vì
đi đường khác nên nó là chỗ mà bất biến dễ trôi mất nhất mà không ai thấy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.control_log import (
    CONTROL_AUDIT_TABLE_NAME,
    ControlAuditAction,
    ControlAuditLog,
    record_control_action,
)
from ket.kernel.datasets.naming import (
    CONTROL_SCHEMA,
    validate_grantable_schema,
    validate_schema_name,
)
from ket.kernel.errors import InvalidSchemaNameError
from ket.kernel.persistence.session import control_session
from ket.kernel.security.dataset_roles import set_local_role_statement
from ket.kernel.security.grants import OWNER_ROLE
from ket.kernel.security.rls import set_search_path_statement

pytestmark = pytest.mark.db

WRITE_ATTEMPTS = (
    f"UPDATE public.{CONTROL_AUDIT_TABLE_NAME} SET action = 'logout'",
    f"DELETE FROM public.{CONTROL_AUDIT_TABLE_NAME}",
    f"TRUNCATE public.{CONTROL_AUDIT_TABLE_NAME}",
    f"DROP TABLE public.{CONTROL_AUDIT_TABLE_NAME}",
    f"ALTER TABLE public.{CONTROL_AUDIT_TABLE_NAME} DROP COLUMN action",
)


def test_table_belongs_to_the_owner_role(owner_engine: Engine) -> None:
    """Điều kiện cần: `REVOKE` chỉ có tác dụng lên vai trò **không** sở hữu bảng."""
    with owner_engine.connect() as connection:
        owner = connection.execute(
            text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
            {"t": CONTROL_AUDIT_TABLE_NAME},
        ).scalar_one()
    assert owner == OWNER_ROLE


def test_runtime_role_can_append_and_read(session_factory: sessionmaker[Session]) -> None:
    """Chỉ-thêm nghĩa là **thêm được** — không phải không dùng được."""
    with control_session(session_factory) as session:
        record_control_action(
            session,
            action=ControlAuditAction.USER_UPDATED,
            subject_label="dò-ghi-nhật-ký",
        )

    with control_session(session_factory) as session:
        found = session.scalars(
            select(func.count())
            .select_from(ControlAuditLog)
            .where(ControlAuditLog.subject_label == "dò-ghi-nhật-ký")
        ).one()
    assert found == 1


@pytest.mark.parametrize("statement", WRITE_ATTEMPTS)
def test_runtime_role_cannot_change_or_remove_entries(app_engine: Engine, statement: str) -> None:
    """Năm đường sửa/xóa, kể cả ba đường DDL — tất cả phải bị PostgreSQL từ chối.

    Kiểm cả `DROP`/`TRUNCATE`/`ALTER` chứ không chỉ `UPDATE`/`DELETE`: một vai
    trò xóa được cả bảng thì "không sửa được dòng nào" chẳng bảo vệ điều gì.
    """
    with app_engine.connect() as connection, pytest.raises(ProgrammingError) as raised:
        connection.exec_driver_sql(statement)

    message = str(raised.value).lower()
    assert "permission denied" in message or "must be owner" in message


def test_only_the_grant_path_may_name_the_control_schema() -> None:
    """`public` hợp lệ ở **đúng một** đường: cấp quyền. Mọi đường khác phải từ chối.

    `validate_grantable_schema` được thêm để `grant_append_only` cấp quyền được
    cho `public.control_audit_log`. Nếu ai đó dùng nó ở đường **định tuyến** vì
    "nó cũng kiểm tên schema mà", một dataset đặt tên `public` sẽ trỏ thẳng vào
    schema điều khiển — mất cả cơ chế cô lập dữ liệu lẫn schema điều khiển.

    Vì vậy hai hàm phải trả lời **khác nhau** cho cùng một đầu vào, và test này
    khóa sự khác nhau đó lại.
    """
    assert validate_grantable_schema(CONTROL_SCHEMA) == CONTROL_SCHEMA
    assert validate_grantable_schema("ds_alpha") == "ds_alpha"

    with pytest.raises(InvalidSchemaNameError):
        validate_schema_name(CONTROL_SCHEMA)
    for reserved in ("pg_catalog", "information_schema", "1_sai"):
        with pytest.raises(InvalidSchemaNameError):
            validate_grantable_schema(reserved)


def test_routing_still_refuses_the_control_schema() -> None:
    """Hệ quả cụ thể của luật trên, đo ở đúng nơi nó quan trọng.

    Cả hai lệnh mở đầu transaction — chọn vai trò và chọn schema — phải từ chối
    `public`, nếu không một request định tuyến được vào schema điều khiển.
    """
    for build in (set_search_path_statement, set_local_role_statement):
        with pytest.raises(InvalidSchemaNameError):
            build(CONTROL_SCHEMA)


def test_runtime_role_holds_exactly_insert_and_select(app_engine: Engine) -> None:
    """Đo **quyền**, không chỉ đo hành vi.

    Test hành vi ở trên có thể xanh vì một lý do sai (bảng đã bị khóa, transaction
    hỏng từ trước). Đọc thẳng `information_schema` cho biết chính xác cụm quyền
    nào đang được cấp, nên nó bắt được cả trường hợp một `GRANT` thừa được thêm
    vào rồi lại bị một lớp khác che đi.
    """
    with app_engine.connect() as connection:
        granted = set(
            connection.execute(
                text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE table_schema = 'public' AND table_name = :t AND grantee = current_user"
                ),
                {"t": CONTROL_AUDIT_TABLE_NAME},
            ).scalars()
        )
    assert granted == {"INSERT", "SELECT"}
