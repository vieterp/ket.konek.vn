"""Cô lập chi nhánh bằng RLS — kể cả qua hàm tổng hợp (RT-04).

Phát hiện RT-04: bộ lọc ở tầng ứng dụng chặn được từng dòng nhưng **không**
chặn được `SUM() OVER ()` trong báo cáo tổng hợp — số tổng của chi nhánh khác
vẫn lọt ra dù không dòng nào hiện lên. Vì thế hàng rào phải nằm ở DB, và test
này kiểm đúng đường rò đó chứ không chỉ kiểm `SELECT *`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.session import dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.tenant import current_branch_scope

pytestmark = pytest.mark.db

BRANCH_A = 101
BRANCH_B = 202


def _write_audit_row(
    factory: sessionmaker[Session], dataset: DatasetRef, branch_id: int, marker: str
) -> None:
    scope = RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=1,
        branch_ids=(branch_id,),
        acting_branch_id=branch_id,
    )
    with unit_of_work(factory, scope) as session:
        session.add(
            AuditLog(
                user_id=1,
                branch_id=branch_id,
                entity_type=marker,
                entity_id="1",
                action=AuditAction.PRINTED.value,
            )
        )


@pytest.fixture(scope="module")
def _seeded(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> DatasetRef:
    _write_audit_row(session_factory, dataset_alpha, BRANCH_A, "rls-a")
    _write_audit_row(session_factory, dataset_alpha, BRANCH_B, "rls-b")
    return dataset_alpha


def _session(
    factory: sessionmaker[Session], dataset: DatasetRef, branches: tuple[int, ...]
) -> AbstractContextManager[Session]:
    return dataset_session(
        factory,
        dataset_schema=dataset.schema_name,
        branch_ids=branches,
        audit=AuditContext(user_id=1, branch_id=branches[0] if branches else None),
    )


def test_rows_of_other_branch_are_invisible(
    session_factory: sessionmaker[Session], _seeded: DatasetRef
) -> None:
    with _session(session_factory, _seeded, (BRANCH_A,)) as session:
        markers = session.execute(
            text("SELECT DISTINCT entity_type FROM audit_log WHERE entity_type LIKE 'rls-%'")
        ).scalars()
        assert set(markers) == {"rls-a"}


def test_window_aggregate_does_not_leak_other_branch(
    session_factory: sessionmaker[Session], _seeded: DatasetRef
) -> None:
    """Đường rò mà bộ lọc tầng ứng dụng không bịt được.

    `SUM() OVER ()` tính trên toàn bộ tập kết quả. Nếu cô lập chỉ nằm ở câu
    `WHERE` do lập trình viên viết, một truy vấn báo cáo quên nó sẽ trả về tổng
    của cả hai chi nhánh trong khi vẫn "trông như" chỉ hiện dòng của mình.
    """
    with _session(session_factory, _seeded, (BRANCH_A,)) as session:
        total = session.execute(
            text("SELECT DISTINCT count(*) OVER () FROM audit_log WHERE entity_type LIKE 'rls-%'")
        ).scalar_one()
    assert total == 1


def test_empty_branch_scope_sees_nothing(
    session_factory: sessionmaker[Session], _seeded: DatasetRef
) -> None:
    """Người dùng chưa được gán chi nhánh = không thấy gì, không phải thấy tất."""
    with _session(session_factory, _seeded, ()) as session:
        assert current_branch_scope(session) == ()
        visible = session.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_type LIKE 'rls-%'")
        ).scalar_one()
    assert visible == 0


def test_cannot_write_rows_for_a_branch_outside_scope(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`WITH CHECK`: không đọc trộm được thì cũng không ghi chèn được."""
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(BRANCH_A,),
        acting_branch_id=BRANCH_A,
    )
    with pytest.raises(ProgrammingError, match="row-level security"):
        with unit_of_work(session_factory, scope) as session:
            session.add(
                AuditLog(
                    user_id=1,
                    branch_id=BRANCH_B,
                    entity_type="rls-forged",
                    entity_id="1",
                    action=AuditAction.PRINTED.value,
                )
            )


def test_system_level_rows_stay_visible(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`branch_id IS NULL` = sự kiện mức hệ thống, không thuộc chi nhánh nào."""
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(BRANCH_A,),
    )
    with unit_of_work(session_factory, scope) as session:
        session.add(
            AuditLog(
                user_id=1,
                branch_id=None,
                entity_type="rls-system",
                entity_id="1",
                action=AuditAction.PRINTED.value,
            )
        )

    with _session(session_factory, dataset_alpha, (BRANCH_B,)) as session:
        visible = session.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_type = 'rls-system'")
        ).scalar_one()
    assert visible == 1


def test_branch_scope_does_not_survive_the_transaction(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """GUC đặt bằng `is_local => true` — nếu không, request sau kế thừa quyền request trước."""
    with _session(session_factory, dataset_alpha, (BRANCH_A,)) as session:
        assert current_branch_scope(session) == (BRANCH_A,)

    session = session_factory()
    try:
        session.begin()
        assert current_branch_scope(session) == ()
    finally:
        session.rollback()
        session.close()
