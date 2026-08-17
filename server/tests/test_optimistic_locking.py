"""Khóa lạc quan: hai lớp, hai tình huống khác nhau (FR-NFR-005).

`test_write_contract_api.py` kiểm lớp thứ nhất — client gửi lên một
`row_version` đã cũ, chặn được ngay khi đọc. Tệp này kiểm lớp thứ hai: hai
transaction cùng đọc **đúng** phiên bản mới nhất rồi cùng ghi. Ở đó không có
phép so sánh nào ở Python bắt được, và thứ phân xử là mệnh đề
`UPDATE … WHERE row_version = ?` do SQLAlchemy sinh ra.

Bỏ `version_id_col` khỏi mixin thì mọi test ở tệp kia vẫn xanh — chỉ tệp này đỏ.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from ket.api.middleware.problem_details import register_problem_handlers
from ket.api.middleware.request_context import RequestContextMiddleware
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import bind_transaction_scope
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


@pytest.mark.db
def test_two_transactions_that_read_the_same_version_cannot_both_write(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Cửa sổ hẹp nhất: cả hai đọc `row_version = 1`, cả hai định ghi.

    Không cần luồng: mở hai `Session` và xen kẽ tay cho ra đúng thứ tự cần —
    và tất định hơn hẳn việc chạy đua bằng thread.
    """
    code = f"CN_{uuid4().hex[:8].upper()}"
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        BranchService(session).create(code=code, name="Bản gốc")

    first = session_factory()
    second = session_factory()
    try:
        for session in (first, second):
            session.begin()
            bind_transaction_scope(
                session,
                dataset_schema=dataset_alpha.schema_name,
                branch_ids=(),
                audit=_scope(dataset_alpha).audit_context(),
            )

        branch_one = first.scalars(select(Branch).where(Branch.code == code)).one()
        branch_two = second.scalars(select(Branch).where(Branch.code == code)).one()
        assert branch_one.row_version == branch_two.row_version, "cả hai phải đọc cùng phiên bản"

        branch_one.name = "Người thứ nhất"
        first.commit()

        branch_two.name = "Người thứ hai"
        with pytest.raises(StaleDataError):
            second.flush()
    finally:
        second.rollback()
        first.close()
        second.close()

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        stored = session.scalars(select(Branch).where(Branch.code == code)).one()

    assert stored.name == "Người thứ nhất", "lệnh ghi thứ hai không được đè lên lệnh thứ nhất"
    assert stored.row_version == 2


def test_a_stale_data_error_becomes_a_409_and_not_a_500() -> None:
    """Handler toàn cục: mọi bảng `RowVersioned` của mọi phase sau đều dựa vào nó.

    Không cần DB — chỉ cần một endpoint ném đúng ngoại lệ mà SQLAlchemy ném.
    Thiếu handler này thì tranh chấp phiên bản hiện ra với người dùng dưới dạng
    "lỗi không mong muốn", còn thao tác của họ thì hoàn toàn hợp lệ.
    """
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)

    @app.post("/tranh-chap")
    def conflict() -> None:
        raise StaleDataError("UPDATE statement on table 'branches' expected to update 1 row(s)")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/tranh-chap")

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "concurrency.row_version_conflict"
    assert body["latest"] is None, "nhánh này không đọc lại DB nên không có bản mới nhất"
    assert body["correlation_id"] is not None
    assert "branches" not in body["detail"], "không lộ thông điệp thô của SQLAlchemy"
