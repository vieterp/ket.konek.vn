"""Cô lập dữ liệu kế toán bằng **quyền**, không chỉ bằng `search_path` (D3, OQ#12).

`tests/test_dataset_routing.py` đã kiểm rằng truy vấn viết tên bảng trần rơi
đúng schema của dataset đang chọn. Đó là cô lập theo **tầm nhìn**, và nó không
chặn được câu truy vấn ghi rõ `ds_beta.branches`. Bộ test này kiểm lớp còn lại:
vai trò đang chạy **không có quyền** trên schema của dataset khác, nên câu truy
vấn định danh đầy đủ cũng bị PostgreSQL từ chối.

Hai test quan trọng nhất — và cũng là hai test sẽ đỏ nếu ai đó gỡ `NOINHERIT`
khỏi `ket_app` hoặc cấp lại quyền bảng cho nó:

* `test_dataset_session_cannot_reach_another_dataset_by_qualified_name`
* `test_no_dataset_table_is_granted_to_the_login_role`
"""

from __future__ import annotations

from unittest import mock

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DataError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.auditing.models import AUDIT_TABLE_NAME
from ket.kernel.datasets.bootstrap import ensure_cluster, ensure_dataset_roles
from ket.kernel.datasets.naming import (
    MAX_DATASET_CODE_LENGTH,
    role_name_for_schema,
    schema_name_for,
)
from ket.kernel.datasets.provisioning import (
    DatasetRef,
    current_revision,
    drop_dataset_schema,
    provision_dataset,
)
from ket.kernel.errors import DatasetRoleNotAdministrableError, InvalidSchemaNameError
from ket.kernel.persistence.session import dataset_session
from ket.kernel.security import dataset_roles
from ket.kernel.security.dataset_roles import CONTROL_GROUP_ROLE
from ket.kernel.security.grants import APP_ROLE, APPEND_ONLY_TABLES, grant_append_only

pytestmark = pytest.mark.db

ACTOR = AuditContext(user_id=1)


def _is_permission_denied(error: ProgrammingError) -> bool:
    """PostgreSQL trả `42501 insufficient_privilege` cho mọi kiểu thiếu quyền.

    Khớp theo **mã lỗi** chứ không theo văn bản: thông điệp đổi theo `lc_messages`
    của cụm, và một test khớp chuỗi tiếng Anh sẽ xanh giả trên máy chạy locale
    khác — đúng loại test tệ hơn không có test.
    """
    return getattr(error.orig, "sqlstate", None) == "42501"


# --------------------------------------------------------------------------
# Lớp quyền: vai trò đăng nhập không với được vào schema dataset
# --------------------------------------------------------------------------


def test_login_role_cannot_read_a_dataset_table_without_switching_role(
    app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """`ket_app` trần — chưa `SET ROLE` — không đọc được bảng nghiệp vụ nào.

    Đây là trạng thái của mọi connection vừa lấy từ pool. Nếu test này xanh
    ngược lại (đọc được), nghĩa là quyền vẫn nằm ở vai trò đăng nhập và việc
    chuyển vai trò chỉ là nghi thức.
    """
    with app_engine.connect() as connection:
        with pytest.raises(ProgrammingError) as error:
            connection.execute(text(f'SELECT count(*) FROM "{dataset_alpha.schema_name}".branches'))
    assert _is_permission_denied(error.value)


def test_dataset_session_cannot_reach_another_dataset_by_qualified_name(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Phiên đã chọn Alpha ghi rõ `ds_beta.branches` → bị từ chối ở tầng quyền.

    Trước D3 câu truy vấn này **chạy được**: `search_path` chỉ quyết định nơi
    tìm tên trần, không cấm định danh đầy đủ. Đó là toàn bộ khoảng cách giữa
    "cô lập bằng quy ước" và "cô lập bằng quyền".
    """
    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=ACTOR,
    ) as session:
        with pytest.raises(ProgrammingError) as error:
            session.execute(text(f'SELECT count(*) FROM "{dataset_beta.schema_name}".branches'))
    assert _is_permission_denied(error.value)


def test_dataset_session_still_reads_its_own_tables(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Đối chứng: siết quyền mà làm hỏng đường hợp lệ thì hai test trên vô nghĩa."""
    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=ACTOR,
    ) as session:
        assert session.execute(text("SELECT count(*) FROM branches")).scalar_one() >= 0


def test_dataset_session_reads_only_the_routing_control_tables(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Sau `SET ROLE`, chỉ hai bảng điều khiển chỉ-đọc còn với tới được.

    `datasets` và `system_metadata` cần cho định tuyến và handshake nên vẫn đọc
    được qua nhóm `ket_control`. Quên cấp cho nhóm thì đường đó hỏng **sau** khi
    chọn dataset — tức là không hỏng ở luồng đăng nhập, nơi test dễ nhìn thấy
    nhất.
    """
    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=ACTOR,
    ) as session:
        assert session.execute(text("SELECT count(*) FROM public.datasets")).scalar_one() >= 1
        assert (
            session.execute(text("SELECT count(*) FROM public.system_metadata")).scalar_one() >= 1
        )


@pytest.mark.parametrize("table", ["users", "auth_sessions", "control_audit_log"])
def test_dataset_role_cannot_touch_identity_tables(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, table: str
) -> None:
    """Vai trò dataset **không** với được bảng danh tính — kể cả để đọc.

    Đây là chiều leo thang đã đóng: mọi truy vấn nghiệp vụ chạy dưới
    `ds_<mã>_app`, nên một lỗ tiêm SQL ở bất kỳ báo cáo nào cũng chạy dưới vai
    trò đó. Còn `INSERT ON auth_sessions` thì lỗ đó tự cấp được phiên dưới danh
    nghĩa bất kỳ ai; còn `UPDATE ON users` thì nó đổi được mật khẩu và tắt được
    cờ 2FA của người khác.

    Đường danh tính không bao giờ chạy dưới vai trò dataset (`control_session`
    cố ý không `SET ROLE`), nên không mất gì.
    """
    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=ACTOR,
    ) as session:
        with pytest.raises(ProgrammingError) as error:
            session.execute(text(f"SELECT count(*) FROM public.{table}"))
        assert "permission denied" in str(error.value)


# --------------------------------------------------------------------------
# Hình dạng vai trò — thứ giữ cho ba test trên còn ý nghĩa
# --------------------------------------------------------------------------


def test_login_role_does_not_inherit_dataset_roles(app_engine: Engine) -> None:
    """`ket_app` phải là `NOINHERIT`.

    Bỏ `NOINHERIT` đi thì `ket_app` tự động có quyền của **mọi** vai trò dataset
    mà nó là thành viên, và cả cơ chế quay về đúng chỗ trước D3 — trong khi các
    test hành vi ở trên vẫn có thể xanh nếu chỉ kiểm qua `dataset_session`.
    """
    with app_engine.connect() as connection:
        inherits = connection.execute(
            text("SELECT rolinherit FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
        ).scalar_one()
    assert inherits is False


def test_dataset_role_is_unprivileged_and_cannot_log_in(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    role = role_name_for_schema(dataset_alpha.schema_name)
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        ).one()
    assert row == (False, False, False, False, False)


def test_dataset_role_inherits_the_control_group(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    role = role_name_for_schema(dataset_alpha.schema_name)
    with owner_engine.connect() as connection:
        member = connection.execute(
            text("SELECT pg_has_role(:role, :group, 'USAGE')"),
            {"role": role, "group": CONTROL_GROUP_ROLE},
        ).scalar_one()
    assert member is True


def test_no_dataset_table_is_granted_to_the_login_role(
    owner_engine: Engine, dataset_alpha: DatasetRef, dataset_beta: DatasetRef
) -> None:
    """Không bảng nào trong schema dataset có mục ACL cho `ket_app`.

    Kiểm ACL trực tiếp thay vì `has_table_privilege`: hàm đó tính cả quyền có
    được **qua tư cách thành viên**, mà `ket_app` thì đúng là thành viên của mọi
    vai trò dataset (điều kiện để `SET ROLE`). Nó sẽ trả `true` và test thành
    vô dụng. Bảng ACL cho biết quyền được cấp cho **ai**, đó mới là câu hỏi.

    Đây là test giữ bất biến cho các phase sau: phase 4 thêm `gl_postings`, và
    một lệnh `GRANT … TO ket_app` chép nhầm từ mã cũ sẽ làm test này đỏ ngay.
    """
    query = text(
        "SELECT n.nspname, c.relname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        # Gồm cả view/matview/bảng phân mảnh/bảng ngoài, không chỉ bảng thường:
        # phase 5 (reporting) sẽ thêm view, phase 4 có thể phân mảnh bảng phát
        # sinh — và một `GRANT … TO ket_app` trên view rò dữ liệu y hệt trên bảng.
        "WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'S', 'v', 'm', 'p', 'f') "
        "AND EXISTS (SELECT 1 FROM aclexplode(c.relacl) a "
        "WHERE a.grantee = CAST(:role AS regrole))"
    )
    with owner_engine.connect() as connection:
        leaked = connection.execute(
            query,
            {
                "schemas": [dataset_alpha.schema_name, dataset_beta.schema_name],
                "role": APP_ROLE,
            },
        ).all()
    assert leaked == []


def test_dataset_schema_usage_is_granted_only_to_its_own_role(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """`USAGE` trên schema là cánh cửa đầu tiên — chỉ vai trò của chính dataset có."""
    with owner_engine.connect() as connection:
        grantees = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT a.grantee::regrole::text FROM pg_namespace n, "
                    "aclexplode(n.nspacl) a WHERE n.nspname = :schema"
                ),
                {"schema": dataset_alpha.schema_name},
            ).all()
        }
    assert APP_ROLE not in grantees
    assert role_name_for_schema(dataset_alpha.schema_name) in grantees


def test_dropping_a_dataset_also_drops_its_role(owner_engine: Engine) -> None:
    """Vai trò sót lại sau khi xóa dataset = mã dùng lại sẽ trúng quyền cũ."""
    code = "temp_role_cleanup"
    provision_dataset(owner_engine, code=code, name="Tạm để kiểm dọn vai trò", scheme="TT99")
    role = role_name_for_schema(schema_name_for(code))

    with owner_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :role"), {"role": role}
            ).scalar_one()
            == 1
        )

    drop_dataset_schema(owner_engine, code)

    with owner_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :role"), {"role": role}
            ).scalar_one()
            == 0
        )


# --------------------------------------------------------------------------
# Đặt tên: chỗ duy nhất mà PostgreSQL hỏng âm thầm
# --------------------------------------------------------------------------


def test_dataset_code_length_leaves_room_for_the_role_suffix() -> None:
    """Trần mã phải trừ cả `ds_` lẫn `_app`, không chỉ tiền tố schema.

    Mã 57 ký tự cho ra schema hợp lệ (60) nhưng tên vai trò 64 ký tự — PostgreSQL
    **cắt còn 63** mà không báo gì. Hai dataset chỉ khác nhau ở ký tự cuối sẽ
    dùng chung một vai trò, tức là mất đúng cơ chế cô lập vừa dựng.
    """
    assert MAX_DATASET_CODE_LENGTH == 56
    longest = "a" * MAX_DATASET_CODE_LENGTH
    assert len(role_name_for_schema(schema_name_for(longest))) == 63

    with pytest.raises(InvalidSchemaNameError):
        schema_name_for("a" * (MAX_DATASET_CODE_LENGTH + 1))


def test_role_name_is_derived_from_the_schema_name() -> None:
    assert role_name_for_schema("ds_alpha") == "ds_alpha_app"


def test_role_name_length_is_checked_on_the_schema_path_too() -> None:
    """Đường vào thứ hai của tên schema cũng phải bị chặn.

    `schema_name_for` chặn ở 56 ký tự, nhưng `role_name_for_schema` còn nhận
    schema từ **bảng đăng ký** và từ `migrations/env.py` (`-x schema=`,
    `KET_DEFAULT_DATASET_SCHEMA`) — hai đường không đi qua `validate_dataset_code`.
    Không có test này thì bỏ hẳn phép kiểm độ dài vẫn xanh (kiểm đột biến M12).
    """
    with pytest.raises(InvalidSchemaNameError):
        role_name_for_schema("ds_" + "a" * 57)


# --------------------------------------------------------------------------
# Khôi phục và vận hành: vai trò phải dựng lại được cho dataset đã tồn tại
# --------------------------------------------------------------------------


def test_ensure_dataset_roles_rebuilds_a_role_lost_with_the_cluster(
    owner_engine: Engine, app_engine: Engine
) -> None:
    """Khôi phục `pg_dump` một database ⇒ có sổ sách, không có vai trò.

    Vai trò là đối tượng cấp cụm nên không nằm trong dump. Trước khi có
    `ensure_dataset_roles`, trạng thái này làm app **không khởi động được** và
    không có đường mã nào sửa: `provision_dataset` từ chối chạy lại với mã đã
    đăng ký. Đây là đúng loại lỗi chỉ xuất hiện ở nơi cài đặt.
    """
    code = "temp_restore_probe"
    dataset = provision_dataset(owner_engine, code=code, name="Dò khôi phục", scheme="TT99")
    role = role_name_for_schema(dataset.schema_name)
    try:
        # Mô phỏng cụm mới: dữ liệu còn, vai trò biến mất.
        with owner_engine.begin() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")

        # `SET LOCAL ROLE` vào vai trò không tồn tại: PostgreSQL trả 22023
        # (`invalid_parameter_value`), psycopg gói thành `DataError` — không phải
        # 42501 như các đường thiếu quyền khác. Đúng thông điệp mà quản trị viên
        # sẽ thấy trên màn hình khi app từ chối khởi động sau khôi phục.
        with app_engine.begin() as connection, pytest.raises(DataError, match=role):
            connection.exec_driver_sql(f"SET LOCAL ROLE {role}")

        assert dataset.schema_name in ensure_dataset_roles(owner_engine)

        # Dựng lại xong thì đường đọc phiên bản schema lúc khởi động chạy lại được.
        assert current_revision(app_engine, dataset.schema_name) is not None
    finally:
        drop_dataset_schema(owner_engine, code)


def test_provisioning_refuses_a_role_it_cannot_administer(
    owner_engine: Engine, superuser_engine: Engine
) -> None:
    """Vai trò do superuser tạo ⇒ `ket_owner` không có ADMIN OPTION ⇒ báo lỗi rõ.

    PostgreSQL 16 đòi ADMIN OPTION mới `ALTER`/`GRANT` được một vai trò. Không
    kiểm trước thì lỗi rơi ra giữa chuỗi DDL dưới dạng *permission denied to
    alter role*, sau khi `CREATE SCHEMA` đã chạy — và cách sửa không suy ra được
    từ thông điệp đó.
    """
    code = "temp_foreign_role"
    role = role_name_for_schema(schema_name_for(code))
    with superuser_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN INHERIT")
    try:
        with pytest.raises(DatasetRoleNotAdministrableError):
            provision_dataset(owner_engine, code=code, name="Vai trò lạ", scheme="TT99")
    finally:
        with superuser_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")


# --------------------------------------------------------------------------
# Đường sửa chữa không được phá bất biến mà nó đi sửa
# --------------------------------------------------------------------------


def _privileges_of(engine: Engine, schema: str, table: str, role: str) -> set[str]:
    """Tập quyền mà `role` có trên một bảng, đọc từ ACL (không tính kế thừa)."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT a.privilege_type "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
                "     aclexplode(c.relacl) a "
                "WHERE n.nspname = :schema AND c.relname = :table "
                "  AND a.grantee = CAST(:role AS regrole)"
            ),
            {"schema": schema, "table": table, "role": role},
        ).all()
    return {row[0] for row in rows}


def test_repair_keeps_append_only_and_read_only_tables_downgraded(
    owner_engine: Engine,
) -> None:
    """`ensure_cluster` cấp `ON ALL TABLES` rồi PHẢI hạ lại đúng hai lớp ngoại lệ.

    Đây là lỗi đã đo được, không phải phòng xa: danh sách bảng chỉ-thêm từng bị
    chép làm hai bản (migration và đường sửa chữa), nên một bảng chỉ-thêm thêm ở
    phase sau bị `ensure_cluster` **âm thầm nâng lên read-write** — nhật ký bất
    biến (FR-NFR-012/013) bị vô hiệu bởi chính đường sửa chữa, và không test nào đỏ.

    Test dựng thêm một bảng chỉ-thêm **thứ hai** (mô phỏng `gl_postings` của phase
    4) để bắt được đúng kiểu hồi quy đó, chứ không chỉ canh mỗi `audit_log`.
    """
    code = "temp_repair_probe"
    dataset = provision_dataset(owner_engine, code=code, name="Dò sửa chữa", scheme="TT99")
    schema = dataset.schema_name
    role = role_name_for_schema(schema)
    second_append_only = "ledger_probe"

    try:
        with owner_engine.begin() as connection:
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema}".{second_append_only} (id bigserial PRIMARY KEY)'
            )
            for statement in grant_append_only(second_append_only, grantee=role, schema=schema):
                connection.exec_driver_sql(statement)

        # Bảng chỉ-thêm thứ hai phải nằm trong nguồn sự thật thì đường sửa chữa
        # mới biết hạ nó — mô phỏng đúng việc phase 4 khai thêm `gl_postings`.
        patched = APPEND_ONLY_TABLES | {second_append_only}
        with mock.patch.object(dataset_roles, "APPEND_ONLY_TABLES", patched):
            ensure_dataset_roles(owner_engine)

        for table in (AUDIT_TABLE_NAME, second_append_only):
            assert _privileges_of(owner_engine, schema, table, role) == {"INSERT", "SELECT"}, (
                f"{table} bị nâng lên read-write bởi đường sửa chữa"
            )
        assert _privileges_of(owner_engine, schema, "alembic_version", role) == {"SELECT"}
        # Đối chứng: bảng thường vẫn phải được cấp đủ quyền.
        assert _privileges_of(owner_engine, schema, "branches", role) == {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
        }
    finally:
        drop_dataset_schema(owner_engine, code)


def test_append_only_registry_matches_what_the_migration_grants(owner_engine: Engine) -> None:
    """Nguồn sự thật và thực tế trong DB phải khớp.

    Nếu ai đó thêm bảng vào `APPEND_ONLY_TABLES` mà quên đổi migration (hoặc
    ngược lại), hai đường cấp quyền lại lệch — đúng lớp lỗi vừa sửa.
    """
    for table in APPEND_ONLY_TABLES:
        assert _privileges_of(owner_engine, "ds_alpha", table, "ds_alpha_app") == {
            "INSERT",
            "SELECT",
        }


def test_ensure_cluster_rebuilds_dataset_roles(owner_engine: Engine, app_engine: Engine) -> None:
    """`ensure_cluster` là điểm-vào-duy-nhất của quy trình khôi phục — phải làm đủ hai nửa.

    Trước đây nó không có nơi gọi nào trong mã và không test nào chạm tới: bỏ hẳn
    lời gọi `ensure_dataset_roles` bên trong mà toàn bộ bộ test vẫn xanh.
    """
    code = "temp_cluster_probe"
    dataset = provision_dataset(owner_engine, code=code, name="Dò ensure_cluster", scheme="TT99")
    role = role_name_for_schema(dataset.schema_name)
    try:
        with owner_engine.begin() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")

        ensure_cluster(owner_engine)

        assert current_revision(app_engine, dataset.schema_name) is not None
    finally:
        drop_dataset_schema(owner_engine, code)


def test_ensure_dataset_roles_revokes_grants_left_to_the_login_role(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Thu hồi quyền mà bản cài **trước D3** đã cấp thẳng cho `ket_app`.

    Cụm đã tạo dữ liệu kế toán bằng mã cũ vẫn còn `GRANT … TO ket_app` ở tầng
    bảng; trên cụm đó `ket_app` trần đọc được sổ của mọi dataset và toàn bộ D3
    không có tác dụng — trong khi bộ test (chạy trên cụm dựng mới) vẫn xanh.
    """
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(f'GRANT SELECT ON "{schema}".branches TO {APP_ROLE}')
    assert _privileges_of(owner_engine, schema, "branches", APP_ROLE) == {"SELECT"}

    ensure_dataset_roles(owner_engine)

    assert _privileges_of(owner_engine, schema, "branches", APP_ROLE) == set()
