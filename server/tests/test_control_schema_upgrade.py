"""Nâng cấp schema điều khiển — diễn tập trên một cụm **đã có dữ liệu**.

Vì sao phải diễn tập thật thay vì tin vào `create_all`: `create_all` tạo bảng
còn thiếu nhưng **không sửa bảng đã có**. Một cụm phiên bản 1 gặp binary phiên
bản 2 sẽ có đủ hai bảng mới và thiếu sạch năm cột mới, và triệu chứng đầu tiên
là `UndefinedColumn` giữa luồng đăng nhập của khách hàng.

Diễn tập chạy trên một **database dùng một lần**, không phải trên database test
dùng chung: nó hạ cấp rồi nâng lại chính `public`, và làm việc đó ở chỗ khác sẽ
xóa cột của những bảng mà test khác đang dựa vào.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from ket.kernel.auditing.control_log import CONTROL_AUDIT_TABLE_NAME
from ket.kernel.datasets.bootstrap import (
    CONTROL_SCHEMA_VERSION,
    _upgrade_path,
    control_schema_version,
    ensure_control_schema,
    ensure_database_roles,
)
from ket.kernel.errors import SchemaVersionMismatchError
from ket.kernel.security.auth_models import SESSION_TABLE_NAME

pytestmark = pytest.mark.db

PROBE_DATABASE = "ket_control_upgrade_probe"

VERSION_1_COLUMNS = frozenset(
    {
        "id",
        "username",
        "password_hash",
        "totp_secret_enc",
        "email",
        "locale",
        "is_active",
        "must_change_password",
        "created_at",
    }
)
"""Hình dạng `users` ở phiên bản 1 — chép từ lát 2A, cố định như một mẩu lịch sử."""

VERSION_2_NEW_COLUMNS = frozenset(
    {
        "totp_required",
        "totp_enrolled_at",
        "totp_last_counter",
        "failed_login_count",
        "locked_until",
    }
)

LEGACY_USER = "ke_toan_cu"


@pytest.fixture
def probe_owner_engine(admin_dsn: str, owner_dsn: str) -> Iterator[Engine]:
    """Database dùng một lần, đã có vai trò nền, trả về engine `ket_owner`.

    Cả hai DSN đều lấy từ fixture rồi **đổi tên database** ở cuối chuỗi. Ghép
    tay từ host/port sẽ mất phần tên đăng nhập mà CI truyền vào.
    """
    admin = create_engine(admin_dsn, poolclass=NullPool).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{PROBE_DATABASE}"')

    superuser = create_engine(f"{admin_dsn.rsplit('/', 1)[0]}/{PROBE_DATABASE}", poolclass=NullPool)
    ensure_database_roles(superuser)
    superuser.dispose()

    owner = create_engine(f"{owner_dsn.rsplit('/', 1)[0]}/{PROBE_DATABASE}", poolclass=NullPool)
    try:
        yield owner
    finally:
        owner.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": PROBE_DATABASE},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}"')
        admin.dispose()


def _downgrade_to_version_1(engine: Engine) -> None:
    """Đưa cụm về đúng hình dạng lát 2A, kèm một người dùng đã tồn tại.

    Người dùng cũ là phần quan trọng: nâng cấp mà mất dữ liệu thì nó không phải
    nâng cấp, và một `DROP TABLE users` lẻn vào bước nâng cấp sẽ xanh mọi test
    chỉ kiểm cấu trúc.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{CONTROL_AUDIT_TABLE_NAME}")
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{SESSION_TABLE_NAME}")
        for column in sorted(VERSION_2_NEW_COLUMNS):
            connection.exec_driver_sql(f"ALTER TABLE public.users DROP COLUMN IF EXISTS {column}")
        connection.execute(
            text(
                "INSERT INTO public.users (username, password_hash) VALUES (:username, 'hash-cũ')"
            ),
            {"username": LEGACY_USER},
        )
        connection.execute(
            text(
                "UPDATE public.system_metadata SET value = '1' WHERE key = 'control_schema_version'"
            )
        )


def _columns_of(engine: Engine, table: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
            ).scalars()
        )


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def test_upgrade_path_covers_every_released_version() -> None:
    """Mọi phiên bản đã phát hành phải có đường đi lên phiên bản hiện tại.

    Cụm v1 phải đi được **hết** chuỗi, không dừng ở nửa đường: một bản cài bỏ
    qua vài lần nâng cấp là chuyện bình thường ở khách hàng chạy offline.
    """
    assert [step.source for step in _upgrade_path("1")] == ["1", "2"]
    assert [step.source for step in _upgrade_path("2")] == ["2"]
    assert _upgrade_path(CONTROL_SCHEMA_VERSION) == ()


def test_upgrade_path_is_empty_for_a_version_this_binary_does_not_know() -> None:
    """DB mới hơn mã nguồn (binary cũ gặp cụm đã nâng cấp) — không có đường đi.

    Trả rỗng thay vì đoán: `_stamp_version` sẽ chặn khởi động, và đó là **một**
    chỗ duy nhất phát ra thông điệp lệch phiên bản.
    """
    assert _upgrade_path("99") == ()


def test_a_version_1_cluster_is_upgraded_in_place(probe_owner_engine: Engine) -> None:
    """Diễn tập đầy đủ: cụm v1 có dữ liệu → chạy binary v2 → đủ bảng, đủ cột, còn dữ liệu."""
    ensure_control_schema(probe_owner_engine)
    _downgrade_to_version_1(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == "1"
    assert _columns_of(probe_owner_engine, "users") == VERSION_1_COLUMNS
    assert CONTROL_AUDIT_TABLE_NAME not in _tables(probe_owner_engine)

    ensure_control_schema(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == CONTROL_SCHEMA_VERSION
    assert _columns_of(probe_owner_engine, "users") == VERSION_1_COLUMNS | VERSION_2_NEW_COLUMNS
    assert {CONTROL_AUDIT_TABLE_NAME, SESSION_TABLE_NAME} <= _tables(probe_owner_engine)

    with probe_owner_engine.connect() as connection:
        row = connection.execute(
            text("SELECT username, totp_required, failed_login_count FROM public.users")
        ).one()
    assert row.username == LEGACY_USER
    assert row.totp_required is False
    assert row.failed_login_count == 0


def test_upgrading_twice_changes_nothing(probe_owner_engine: Engine) -> None:
    """Chạy lại được: nâng cấp bị ngắt giữa chừng phải chạy tiếp mà không sửa tay DB."""
    ensure_control_schema(probe_owner_engine)
    _downgrade_to_version_1(probe_owner_engine)

    ensure_control_schema(probe_owner_engine)
    ensure_control_schema(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == CONTROL_SCHEMA_VERSION


def test_new_table_gets_its_grants_on_an_upgraded_cluster(probe_owner_engine: Engine) -> None:
    """Nửa dễ quên của nâng cấp: bảng mới có mặt nhưng **không ai dùng được**.

    `create_all` tạo bảng, nó không cấp quyền. Nếu bước cấp quyền chỉ chạy trên
    cụm mới, một bản cài đã nâng cấp sẽ đăng nhập được tới lúc ghi dòng nhật ký
    đầu tiên rồi đổ với "permission denied".
    """
    ensure_control_schema(probe_owner_engine)
    _downgrade_to_version_1(probe_owner_engine)
    ensure_control_schema(probe_owner_engine)

    with probe_owner_engine.connect() as connection:
        granted = set(
            connection.execute(
                text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE table_schema = 'public' AND table_name = :t AND grantee = 'ket_app'"
                ),
                {"t": CONTROL_AUDIT_TABLE_NAME},
            ).scalars()
        )
    assert granted == {"INSERT", "SELECT"}


def test_a_provisioned_cluster_without_a_version_row_refuses_to_be_relabelled(
    probe_owner_engine: Engine,
) -> None:
    """Đường dán nhãn khống thứ hai: cụm **đủ bảng** nhưng **mất dòng phiên bản**.

    Đo được trước khi sửa: `trước: phiên bản=None, cột mới=[]` →
    `sau: phiên bản=2, cột mới=[]`. `create_all` bỏ qua bảng đã có, bước nâng cấp
    bị bỏ qua vì "không đọc được phiên bản cũ", rồi `_stamp_version` INSERT nhãn
    hiện tại — DB nói phiên bản 2 trong khi `users` còn thiếu năm cột.

    Dòng phiên bản mất là ca thật: một bản dump thiếu, hoặc một lệnh `DELETE`
    trên `system_metadata` gõ nhầm.
    """
    ensure_control_schema(probe_owner_engine)
    _downgrade_to_version_1(probe_owner_engine)
    with probe_owner_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM public.system_metadata WHERE key = 'control_schema_version'")
        )

    with pytest.raises(SchemaVersionMismatchError):
        ensure_control_schema(probe_owner_engine)

    # Và quan trọng không kém: nó **không** để lại nhãn phiên bản khống.
    assert control_schema_version(probe_owner_engine) is None
    assert _columns_of(probe_owner_engine, "users") == VERSION_1_COLUMNS


def test_an_empty_cluster_is_still_built_from_scratch(probe_owner_engine: Engine) -> None:
    """Mặt kia của cùng luật: cụm **trống** vẫn phải dựng được, không bị chặn nhầm."""
    ensure_control_schema(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == CONTROL_SCHEMA_VERSION
    assert {CONTROL_AUDIT_TABLE_NAME, SESSION_TABLE_NAME} <= _tables(probe_owner_engine)


def test_an_unknown_version_refuses_to_be_relabelled(probe_owner_engine: Engine) -> None:
    """Bất biến của lát 2A phải sống sót: không tự dán nhãn phiên bản.

    Một cụm mang phiên bản mà binary này không biết đường nâng cấp thì phải
    **dừng**, chứ không được ghi đè số phiên bản lên như thể đã nâng cấp xong.
    """
    ensure_control_schema(probe_owner_engine)
    with probe_owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE public.system_metadata SET value = '99' WHERE key = 'control_schema_version'"
            )
        )

    with pytest.raises(SchemaVersionMismatchError):
        ensure_control_schema(probe_owner_engine)


VERSION_3_NEW_COLUMNS = frozenset({"scope"})
"""Cột `auth_sessions.scope` — phạm vi phiên (lát 2B-1b, quyết định E2)."""


def _downgrade_to_version_2(engine: Engine) -> int:
    """Đưa cụm về hình dạng lát 2B-1a, kèm một phiên đang mở. Trả id phiên đó.

    Phiên đang mở là phần quan trọng: nâng cấp mà biến mọi phiên đầy đủ thành
    phiên hạn chế sẽ đuổi cả văn phòng ra ngoài giữa buổi làm việc — đúng loại
    hỏng mà một `DEFAULT` thiếu sẽ gây ra và không test cấu trúc nào bắt được.
    """
    with engine.begin() as connection:
        for column in sorted(VERSION_3_NEW_COLUMNS):
            connection.exec_driver_sql(
                f"ALTER TABLE public.{SESSION_TABLE_NAME} DROP COLUMN IF EXISTS {column}"
            )
        connection.exec_driver_sql(
            f"ALTER TABLE public.{SESSION_TABLE_NAME} "
            "DROP CONSTRAINT IF EXISTS ck_auth_sessions_scope_known"
        )
        session_id = connection.execute(
            text(
                f"INSERT INTO public.{SESSION_TABLE_NAME} "
                "(token_hash, user_id, expires_at) "
                "VALUES (decode('00ff', 'hex'), 1, now() + interval '1 day') RETURNING id"
            )
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE public.system_metadata SET value = '2' WHERE key = 'control_schema_version'"
            )
        )
    return int(session_id)


def test_a_version_2_cluster_gains_session_scope_without_locking_anyone_out(
    probe_owner_engine: Engine,
) -> None:
    """Diễn tập 2 → 3: cột mới có, và phiên đang mở vẫn là phiên **đầy đủ**."""
    ensure_control_schema(probe_owner_engine)
    session_id = _downgrade_to_version_2(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == "2"
    assert "scope" not in _columns_of(probe_owner_engine, SESSION_TABLE_NAME)

    ensure_control_schema(probe_owner_engine)

    assert control_schema_version(probe_owner_engine) == CONTROL_SCHEMA_VERSION
    assert "scope" in _columns_of(probe_owner_engine, SESSION_TABLE_NAME)

    with probe_owner_engine.connect() as connection:
        scope = connection.execute(
            text(f"SELECT scope FROM public.{SESSION_TABLE_NAME} WHERE id = :id"),
            {"id": session_id},
        ).scalar_one()
        constrained = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_auth_sessions_scope_known'"
            )
        ).scalar_one()
    assert scope == "full"
    # Ràng buộc `CHECK` là thứ chặn một giá trị phạm vi bịa lọt vào cột.
    assert constrained == 1


LEGACY_CONTROL_GROUP_GRANTS = (
    "GRANT SELECT, INSERT, UPDATE ON public.users TO ket_control",
    "GRANT USAGE, SELECT ON SEQUENCE public.users_id_seq TO ket_control",
    "GRANT SELECT, INSERT, UPDATE ON public.auth_sessions TO ket_control",
    "GRANT USAGE, SELECT ON SEQUENCE public.auth_sessions_id_seq TO ket_control",
    "GRANT INSERT, SELECT ON public.control_audit_log TO ket_control",
)
"""Đúng những lệnh mà lát 2B-1a cấp cho nhóm `ket_control`.

Chép cứng như một mẩu lịch sử, không sinh lại từ mã hiện tại: đây là hình dạng
của **cụm khách hàng đã cài**, và nếu sinh từ mã thì test sẽ tự đổi theo mã và
không còn kiểm gì."""


def test_an_installed_cluster_loses_the_identity_grants_it_used_to_have(
    probe_owner_engine: Engine,
) -> None:
    """Nâng cấp phải **thu hồi** quyền cũ, không chỉ ngừng cấp quyền mới.

    Bất biến "vai trò dataset không với được bảng danh tính" trên cụm dựng mới
    đúng vì quyền chưa bao giờ được cấp. Trên cụm **đã cài bằng mã cũ** thì quyền
    còn nguyên, và không có `0002` nào gỡ chúng ra — nên nếu `ensure_control_schema`
    không thu hồi, mọi vai trò dataset vẫn `INSERT` được vào `auth_sessions`, tức
    là tự cấp được phiên dưới danh nghĩa bất kỳ ai. Test dựng mới **không** thấy
    điều đó; đây là chỗ duy nhất thấy.
    """
    ensure_control_schema(probe_owner_engine)
    with probe_owner_engine.begin() as connection:
        for statement in LEGACY_CONTROL_GROUP_GRANTS:
            connection.exec_driver_sql(statement)

    assert _control_group_privileges(probe_owner_engine) != set()

    ensure_control_schema(probe_owner_engine)

    remaining = _control_group_privileges(probe_owner_engine)
    # Chỉ còn quyền đọc hai bảng định tuyến; không còn gì trên bảng danh tính.
    assert remaining == {("datasets", "SELECT"), ("system_metadata", "SELECT")}


def _control_group_privileges(engine: Engine) -> set[tuple[str, str]]:
    """Quyền bảng mà nhóm `ket_control` đang giữ, đo từ `information_schema`."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                "WHERE grantee = 'ket_control' AND table_schema = 'public'"
            )
        ).all()
    return {(row[0], row[1]) for row in rows}
