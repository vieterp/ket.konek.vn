"""Cơ cấu tổ chức dạng cây trên `branches` (FR-SYS-050, FR-NFR-030).

Bảng `branches` ra đời ở phase 2 làm neo cô lập dữ liệu và được cây hóa ở phase
3. Tệp này kiểm phần **cây**: dựng nút, nối cấp, chuyển nhánh, chặn chu trình.

Hai thứ cố ý **không** kiểm ở đây, ghi ra để không ai tưởng chúng đã có cổng:

* Chi nhánh có sẵn từ trước migration `0002` thành nút gốc — đó là hành vi của
  `_upgrade_branches`, cần một bộ test chạy migration trên dữ liệu cũ (chưa có;
  hiện kiểm bằng tay ở mỗi lần review).
* Phạm vi RLS **không** tự nới theo cây (được xem trụ sở ≠ được xem lương chi
  nhánh con). Đó là bất biến của `user_branches`, và `subtree_of` cố ý không
  đụng tới — xem docstring của nó.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import BranchNotFoundError, MasterDataCycleError
from ket.kernel.master_data.tree_path import level_of
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def test_a_branch_without_a_parent_is_a_root_node(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        branch = BranchService(session).create(code="TRU-SO", name="Trụ sở chính")

        assert branch.path == f"{branch.id}."
        assert branch.level == 1
        assert branch.is_dependent_accounting is True


def test_a_child_branch_extends_the_parent_path(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = BranchService(session)
        head = service.create(code="TS-CAY", name="Trụ sở")
        region = service.create(code="MIEN-BAC", name="Miền Bắc", parent_id=head.id)
        office = service.create(
            code="CN-HN",
            name="Chi nhánh Hà Nội",
            parent_id=region.id,
            is_dependent_accounting=False,
            tax_code="0123456789-001",
        )

        assert office.path.startswith(head.path)
        assert level_of(office.path) == 3 == office.level
        assert office.tax_code == "0123456789-001"


def test_moving_a_branch_carries_its_subtree(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = BranchService(session)
        old_parent = service.create(code="CU", name="Trực thuộc cũ")
        new_parent = service.create(code="MOI", name="Trực thuộc mới")
        moved = service.create(code="DI-CHUYEN", name="Đơn vị chuyển", parent_id=old_parent.id)
        child = service.create(code="CAP-DUOI", name="Cấp dưới", parent_id=moved.id)

        service.move(moved.id, new_parent_id=new_parent.id, expected_row_version=moved.row_version)

        subtree = service.subtree_of(moved.id)
        assert {branch.code for branch in subtree} == {"DI-CHUYEN", "CAP-DUOI"}
        for branch in subtree:
            assert branch.path.startswith(new_parent.path)
            assert branch.level == level_of(branch.path)
        assert service.get(child.id).level == 3


def test_moving_a_branch_under_its_own_subordinate_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = BranchService(session)
        head = service.create(code="VONG-TS", name="Trụ sở")
        below = service.create(code="VONG-CD", name="Cấp dưới", parent_id=head.id)

        with pytest.raises(MasterDataCycleError):
            service.move(head.id, new_parent_id=below.id, expected_row_version=head.row_version)


def test_an_unknown_branch_is_a_business_error(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(BranchNotFoundError):
            BranchService(session).get(10_000_000)
