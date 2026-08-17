"""Cô lập nhiều dữ liệu kế toán bằng schema-per-dataset (FR-SYS-001, RT-17/ADR-017).

Đây là bất biến mà một phần mềm kế toán không được phép hỏng lặng lẽ: dữ liệu
của doanh nghiệp A không bao giờ được lọt vào sổ của doanh nghiệp B. Cơ chế là
`search_path` theo transaction, nên test kiểm cả ba mặt: dữ liệu tách, đánh số
tách, nhật ký tách.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.models import Dataset, SystemMetadata, User
from ket.kernel.datasets.naming import (
    RESERVED_SCHEMA_NAMES,
    schema_name_for,
    validate_schema_name,
)
from ket.kernel.datasets.provisioning import DatasetRef, current_revision
from ket.kernel.datasets.service import list_datasets, resolve_dataset
from ket.kernel.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    InvalidSchemaNameError,
)
from ket.kernel.numbering.models import NumberSequence
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.session import dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

ACTOR = AuditContext(user_id=1)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def test_each_dataset_gets_its_own_schema_at_the_same_revision(
    owner_engine: Engine, dataset_alpha: DatasetRef, dataset_beta: DatasetRef
) -> None:
    assert dataset_alpha.schema_name != dataset_beta.schema_name
    assert current_revision(owner_engine, dataset_alpha.schema_name) == current_revision(
        owner_engine, dataset_beta.schema_name
    )


def test_data_written_in_one_dataset_is_absent_from_the_other(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        BranchService(session).create(code="ONLY-ALPHA", name="Chỉ có ở Alpha")

    with dataset_session(
        session_factory,
        dataset_schema=dataset_beta.schema_name,
        branch_ids=(),
        audit=ACTOR,
    ) as session:
        found = session.execute(
            text("SELECT count(*) FROM branches WHERE code = 'ONLY-ALPHA'")
        ).scalar_one()
    assert found == 0


def test_numbering_counters_are_independent(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Hai doanh nghiệp đánh số chứng từ độc lập — hệ quả trực tiếp của ADR-017."""
    for dataset, next_value in ((dataset_alpha, 500), (dataset_beta, 1)):
        with unit_of_work(session_factory, _scope(dataset)) as session:
            session.add(NumberSequence(scope_key="cash_receipt/2026", next_value=next_value))

    for dataset, expected in ((dataset_alpha, 500), (dataset_beta, 1)):
        with dataset_session(
            session_factory,
            dataset_schema=dataset.schema_name,
            branch_ids=(),
            audit=ACTOR,
        ) as session:
            value = session.execute(
                text(
                    "SELECT next_value FROM number_sequences WHERE scope_key = 'cash_receipt/2026'"
                )
            ).scalar_one()
        assert value == expected


def test_audit_log_is_per_dataset(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Nhật ký của doanh nghiệp này không xuất hiện trong nhật ký doanh nghiệp kia."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        BranchService(session).create(code="AUDIT-SPLIT", name="Kiểm tra tách nhật ký")

    counts = {}
    for dataset in (dataset_alpha, dataset_beta):
        with dataset_session(
            session_factory,
            dataset_schema=dataset.schema_name,
            branch_ids=(),
            audit=ACTOR,
        ) as session:
            counts[dataset.code] = session.execute(
                text("SELECT count(*) FROM audit_log WHERE new_values->>'code' = 'AUDIT-SPLIT'")
            ).scalar_one()
    assert counts == {dataset_alpha.code: 1, dataset_beta.code: 0}


def test_dataset_table_names_never_collide_with_control_tables() -> None:
    """`search_path` là `ds_x, public` — trùng tên nghĩa là truy vấn có thể trúng nhầm bảng.

    Nếu một ngày có bảng `users` trong schema dataset, mọi truy vấn `users`
    không định danh sẽ trúng bảng dataset ở chỗ này và trúng bảng điều khiển ở
    chỗ khác, tùy schema nào đã chạy migration. Khóa bất biến ngay từ bây giờ.
    """
    control_tables = {Dataset.__tablename__, User.__tablename__, SystemMetadata.__tablename__}
    dataset_tables = set(DatasetBase.metadata.tables)
    assert control_tables.isdisjoint(dataset_tables)


def test_resolve_dataset_rejects_unknown_code(owner_engine: Engine) -> None:
    """Mã bịa trong token không được biến thành `search_path` trỏ vào hư không."""
    with pytest.raises(DatasetNotFoundError):
        resolve_dataset(owner_engine, "khong_ton_tai")


def test_provisioning_refuses_duplicate_code(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    from ket.kernel.datasets.provisioning import provision_dataset

    with pytest.raises(DatasetAlreadyExistsError):
        provision_dataset(owner_engine, code=dataset_alpha.code, name="Trùng mã", scheme="TT200")


def test_list_datasets_returns_registered_datasets(
    owner_engine: Engine, dataset_alpha: DatasetRef, dataset_beta: DatasetRef
) -> None:
    codes = {dataset.code for dataset in list_datasets(owner_engine)}
    assert {dataset_alpha.code, dataset_beta.code} <= codes


@pytest.mark.parametrize(
    "code",
    [
        "Alpha",  # chữ hoa → PostgreSQL cần trích dẫn, dễ lệch giữa hai đường code
        "alpha; DROP SCHEMA ds_alpha",  # tiêm SQL qua identifier
        "alpha kho",  # khoảng trắng
        'alpha"',  # thoát khỏi dấu trích dẫn
        "_alpha",  # gạch dưới đầu — dành cho tên nội bộ
        "a" * 70,  # vượt trần identifier: PostgreSQL CẮT BỚT âm thầm, hai mã
        # khác nhau có thể trỏ về cùng một schema
    ],
)
def test_dataset_code_validation_rejects_dangerous_input(code: str) -> None:
    """Tên schema là identifier — không tham số hóa được, nên phải chặn bằng whitelist."""
    with pytest.raises(InvalidSchemaNameError):
        schema_name_for(code)


@pytest.mark.parametrize("code", ["public", "pg_catalog", "1alpha"])
def test_reserved_names_are_neutralised_by_the_prefix(code: str) -> None:
    """Tiền tố `ds_` là thứ khiến mã trùng tên schema hệ thống vẫn an toàn.

    `public` là mã dữ liệu kế toán hợp lệ vì nó thành `ds_public`, không phải
    `public`. Bất biến cần khóa là **kết quả** không bao giờ là tên dành riêng.
    """
    schema = schema_name_for(code)
    assert schema.startswith("ds_")
    assert schema not in RESERVED_SCHEMA_NAMES


@pytest.mark.parametrize("schema", ["public", "pg_catalog", "pg_temp", "information_schema"])
def test_reserved_schema_names_are_rejected_directly(schema: str) -> None:
    """Đường vào thứ hai: schema đọc từ bảng `datasets` hoặc từ dòng lệnh migration."""
    with pytest.raises(InvalidSchemaNameError):
        validate_schema_name(schema)
