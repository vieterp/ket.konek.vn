"""Nhật ký bất biến — ép ở tầng DB, không phải bằng kỷ luật lập trình (RT-02).

Phát hiện RT-02 của phiên red team: `REVOKE UPDATE ON audit_log` **không có tác
dụng** nếu vai trò runtime là chủ sở hữu bảng — chủ sở hữu tự bỏ qua REVOKE của
chính mình. Bộ test này là bằng chứng cơ chế tách vai trò đang thật sự chặn,
chạy lại mỗi lần CI, chứ không phải một dòng trong tài liệu kiến trúc.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import AuditContextMissingError
from ket.kernel.persistence.session import dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch

pytestmark = pytest.mark.db

BRANCH_ID = 1
ACTOR = AuditContext(user_id=7, branch_id=BRANCH_ID, client_info="pytest")


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=ACTOR.user_id,
        branch_ids=(BRANCH_ID,),
        acting_branch_id=BRANCH_ID,
        client_info="pytest",
    )


def test_app_role_has_no_privileges_that_defeat_the_mechanism(app_engine: Engine) -> None:
    """`ket_app` không owner, không superuser, không BYPASSRLS.

    Ba thuộc tính này là **điều kiện cần** của cả nhật ký bất biến lẫn cô lập
    chi nhánh. Mất bất kỳ cái nào, cả hai cơ chế im lặng vô hiệu.
    """
    with app_engine.connect() as connection:
        row = connection.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False


def test_app_role_owns_no_table(app_engine: Engine, dataset_alpha: DatasetRef) -> None:
    """Chân thứ ba của cơ chế: **không sở hữu bảng nào**.

    Chủ sở hữu tự bỏ qua `REVOKE` của chính mình và tự bỏ qua policy RLS. Nếu
    một migration tương lai tạo bảng bằng vai trò runtime, hai cơ chế bảo vệ im
    lặng vô hiệu trong khi mọi test hành vi khác vẫn xanh.
    """
    with app_engine.connect() as connection:
        owned = connection.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname IN (:schema, 'public') AND tableowner = current_user"
            ),
            {"schema": dataset_alpha.schema_name},
        ).scalars()
        assert not list(owned)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'deleted'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "DROP TABLE audit_log",
        "ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY",
    ],
)
def test_runtime_role_cannot_tamper_with_audit_log(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    statement: str,
) -> None:
    """Mọi đường sửa/xóa/vô hiệu hóa nhật ký đều bị PostgreSQL từ chối."""
    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(BRANCH_ID,),
        audit=ACTOR,
    ) as session:
        with pytest.raises(ProgrammingError) as error:
            session.execute(text(statement))
        assert (
            "permission denied" in str(error.value).lower()
            or "must be owner" in str(error.value).lower()
        )


def test_runtime_role_can_append_and_read(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chỉ thêm và đọc — đúng hai quyền mà nhật ký cần."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.add(
            AuditLog(
                user_id=ACTOR.user_id,
                branch_id=BRANCH_ID,
                entity_type="smoke",
                entity_id="1",
                action=AuditAction.PRINTED.value,
            )
        )

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(BRANCH_ID,),
        audit=ACTOR,
    ) as session:
        count = session.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_type = 'smoke'")
        ).scalar_one()
    assert count == 1


def test_change_writes_audit_row_automatically(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Không service nào phải nhớ gọi — listener bắt mọi thay đổi qua ORM."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.add(Branch(code="AUTO-1", name="Chi nhánh tự động"))

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(BRANCH_ID,),
        audit=ACTOR,
    ) as session:
        row = session.execute(
            text(
                "SELECT action, user_id, new_values FROM audit_log "
                "WHERE entity_type = 'branches' AND new_values->>'code' = 'AUTO-1'"
            )
        ).one()
    assert row.action == AuditAction.CREATED.value
    assert row.user_id == ACTOR.user_id
    assert row.new_values["name"] == "Chi nhánh tự động"


def test_update_records_only_changed_columns(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ghi diff, không ghi cả dòng (`docs/srs/01` §13 Q3 — chống phình bảng)."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.add(Branch(code="DIFF-1", name="Tên cũ"))

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        branch = session.execute(text("SELECT id FROM branches WHERE code = 'DIFF-1'")).scalar_one()
        session.get(Branch, branch).name = "Tên mới"

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(BRANCH_ID,),
        audit=ACTOR,
    ) as session:
        row = session.execute(
            text(
                "SELECT old_values, new_values FROM audit_log "
                "WHERE entity_type = 'branches' AND action = 'updated' "
                "AND new_values->>'name' = 'Tên mới'"
            )
        ).one()
    assert row.old_values == {"name": "Tên cũ"}
    assert row.new_values == {"name": "Tên mới"}


def test_rollback_leaves_no_orphan_audit_row(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Nhật ký ghi trong cùng transaction → không kể lại thao tác chưa từng xảy ra."""
    with pytest.raises(RuntimeError, match="hỏng giữa chừng"):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            session.add(Branch(code="ROLLBACK-1", name="Sẽ bị hủy"))
            session.flush()
            raise RuntimeError("Nghiệp vụ hỏng giữa chừng")

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(BRANCH_ID,),
        audit=ACTOR,
    ) as session:
        audit_rows = session.execute(
            text("SELECT count(*) FROM audit_log WHERE new_values->>'code' = 'ROLLBACK-1'")
        ).scalar_one()
        branch_rows = session.execute(
            text("SELECT count(*) FROM branches WHERE code = 'ROLLBACK-1'")
        ).scalar_one()
    assert audit_rows == 0
    assert branch_rows == 0


def test_change_without_actor_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Fail-closed: thà chặn thao tác còn hơn ghi một thay đổi không có vết."""
    session = session_factory()
    try:
        session.begin()
        session.execute(text(f'SET LOCAL search_path TO "{dataset_alpha.schema_name}", public'))
        session.add(Branch(code="NO-ACTOR", name="Không có người thực hiện"))
        with pytest.raises(AuditContextMissingError):
            session.flush()
    finally:
        session.rollback()
        session.close()
