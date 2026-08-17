"""Nhật ký không được kể lại thao tác đã bị hủy.

Lỗi mà bộ test này khóa lại: `before_flush` chụp diff vào `session.info`, còn
`after_flush` mới ghi và dọn. Một flush **hỏng** chạy nửa đầu rồi dừng, để diff
nằm lại; lần flush thành công kế tiếp trên cùng `Session` ghi luôn cả diff cũ.
Kết quả là một dòng **vĩnh viễn** trong bảng chỉ-thêm, mô tả một bản ghi chưa
từng tồn tại — và không ai, kể cả chủ sở hữu bảng, được phép sửa lại.

Đường tái hiện không hề exotic: `begin_nested()` (SAVEPOINT) quanh một lệnh ghi
có thể đụng ràng buộc là đúng khuôn mẫu mà idempotency (bước 9) và optimistic
locking (bước 10) sẽ dùng.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

ACTOR = AuditContext(user_id=1)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def _audit_codes(
    factory: sessionmaker[Session], dataset: DatasetRef, prefix: str
) -> list[str | None]:
    with dataset_session(
        factory, dataset_schema=dataset.schema_name, branch_ids=(), audit=ACTOR
    ) as session:
        return list(
            session.execute(
                text(
                    "SELECT new_values->>'code' FROM audit_log "
                    "WHERE new_values->>'code' LIKE :prefix ORDER BY id"
                ),
                {"prefix": f"{prefix}%"},
            ).scalars()
        )


def _branch_codes(factory: sessionmaker[Session], dataset: DatasetRef, prefix: str) -> list[str]:
    with dataset_session(
        factory, dataset_schema=dataset.schema_name, branch_ids=(), audit=ACTOR
    ) as session:
        return list(
            session.execute(
                text("SELECT code FROM branches WHERE code LIKE :prefix ORDER BY code"),
                {"prefix": f"{prefix}%"},
            ).scalars()
        )


def test_savepoint_rollback_does_not_leak_into_the_next_flush(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """SAVEPOINT hủy → diff của nó phải biến mất, không trôi sang lần ghi sau."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        BranchService(session).create(code="SP-1", name="bản ghi thật")
        session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            BranchService(session).create(code="SP-1", name="trùng mã, sẽ bị hủy")
            session.flush()

        BranchService(session).create(code="SP-2", name="bản ghi thật thứ hai")

    assert _branch_codes(session_factory, dataset_alpha, "SP-") == ["SP-1", "SP-2"]
    # Đúng hai dòng nhật ký. Trước khi sửa, ở đây có ba: "SP-1" xuất hiện hai
    # lần, lần thứ hai mô tả bản ghi đã bị SAVEPOINT hủy.
    assert _audit_codes(session_factory, dataset_alpha, "SP-") == ["SP-1", "SP-2"]


def test_failed_flush_then_rollback_then_reuse_leaks_nothing(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bắt lỗi → `rollback()` → dùng lại `Session`: đường phục hồi thủ công."""
    session = session_factory()
    scope = _scope(dataset_alpha)
    try:
        session.begin()
        from ket.kernel.persistence.session import bind_transaction_scope

        bind_transaction_scope(
            session,
            dataset_schema=scope.dataset_schema,
            branch_ids=scope.branch_ids,
            audit=scope.audit_context(),
        )
        BranchService(session).create(code="RB-1", name="đầu tiên")
        with pytest.raises(IntegrityError):
            BranchService(session).create(code="RB-1", name="trùng mã")
        session.rollback()

        session.begin()
        bind_transaction_scope(
            session,
            dataset_schema=scope.dataset_schema,
            branch_ids=scope.branch_ids,
            audit=scope.audit_context(),
        )
        BranchService(session).create(code="RB-2", name="sau khi phục hồi")
        session.commit()
    finally:
        session.close()

    assert _branch_codes(session_factory, dataset_alpha, "RB-") == ["RB-2"]
    assert _audit_codes(session_factory, dataset_alpha, "RB-") == ["RB-2"]


def test_created_snapshot_holds_real_values_not_pre_flush_nulls(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ảnh "sau" của bản ghi mới phải là giá trị THẬT sau khi ghi.

    Chụp ở `before_flush` thì khóa chính chưa cấp và default chưa áp, nên nhật
    ký ghi `id: null`, `is_active: null` cho một dòng thực tế là `(7, true)` —
    kiểm toán viên dựng lại "người dùng đã tạo gì" sẽ nhận số liệu sai ở mọi
    cột có default. Từ phase 4 thì đó là `status`, `posted`, `row_version`.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        BranchService(session).create(code="SNAP-1", name="kiểm tra ảnh chụp")

    with dataset_session(
        session_factory, dataset_schema=dataset_alpha.schema_name, branch_ids=(), audit=ACTOR
    ) as session:
        row = session.execute(
            text(
                "SELECT a.new_values AS snapshot, a.entity_id, b.id AS real_id, "
                "       b.is_active AS real_is_active "
                "FROM audit_log a JOIN branches b ON b.code = a.new_values->>'code' "
                "WHERE a.new_values->>'code' = 'SNAP-1'"
            )
        ).one()

    assert row.snapshot["id"] == row.real_id
    assert row.snapshot["is_active"] is row.real_is_active is True
    assert row.entity_id == str(row.real_id)
