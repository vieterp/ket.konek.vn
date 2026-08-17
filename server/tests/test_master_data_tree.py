"""Khung danh mục dùng chung: cây, phạm vi mã, xóa có điều kiện.

Đây là lớp mà mọi phân hệ phía sau dựa vào (`docs/srs/01` §3.1). Test bám vào
bốn luật dễ hỏng nhất, và mỗi luật hỏng theo một kiểu khác nhau:

* **Cây** (FR-SYS-011) — hỏng thì báo cáo phân cấp cộng thiếu hoặc cộng dư.
* **Mã duy nhất theo phạm vi** (BR-SYS-01) — hỏng thì hai bản ghi cùng mã và
  chứng từ trỏ nhầm.
* **`uid` ổn định** (RT-19) — hỏng thì việc nối dữ liệu giữa các bản cài gộp
  nhầm hai đối tượng.
* **Không xóa khi đã dùng** (BR-SYS-02) — hỏng thì sổ năm trước mất tên danh mục.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    MasterDataCycleError,
    MasterDataGroupNotPostableError,
    MasterDataInUseError,
    MasterDataNotFoundError,
    MasterDataParentScopeError,
)
from ket.kernel.master_data.models import CostObject, ExpenseItem
from ket.kernel.master_data.service import MasterDataService, depth_of
from ket.kernel.master_data.tree_path import level_of
from ket.kernel.master_data.usage import record_use, usage_count_of
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=branch_ids)


def _chain(service: MasterDataService[CostObject], depth: int, prefix: str) -> list[CostObject]:
    """Một nhánh thẳng `depth` cấp — cây thật của khách hàng sâu ít nhất 5 cấp."""
    nodes: list[CostObject] = []
    parent_id: int | None = None
    for level in range(depth):
        node = service.create(
            code=f"{prefix}-{level}",
            name=f"Cấp {level}",
            parent_id=parent_id,
            is_group=level < depth - 1,
        )
        nodes.append(node)
        parent_id = node.id
    return nodes


def test_a_six_level_tree_gets_consistent_paths_and_levels(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        nodes = _chain(service, 6, "SAU")

        for index, node in enumerate(nodes):
            assert node.level == index + 1
            assert depth_of(node) == node.level
        assert nodes[-1].path.startswith(nodes[0].path)
        assert level_of(nodes[-1].path) == 6


def test_moving_a_branch_rewrites_every_descendant_in_one_statement(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tiêu chí phase 3: đổi nút cha cập nhật cả nhánh bằng **một** câu UPDATE.

    Đếm câu lệnh chứ không chỉ kiểm kết quả: một vòng lặp Python cập nhật từng
    dòng cũng cho ra `path` đúng, và nó sẽ đúng cho tới ngày có ai chuyển một
    nhóm vật tư mười nghìn dòng.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        nodes = _chain(service, 4, "CHUYEN")
        new_root = service.create(code="CHUYEN-DICH", name="Gốc mới", is_group=True)
        moved, moved_version = nodes[1].id, nodes[1].row_version
        # Phiên bản của một nút **con** trước khi chuyển — nó phải tăng theo, vì
        # `path` của nó đổi và một client đang giữ bản cũ không được lưu đè im
        # lặng (FR-NFR-005).
        descendant_id = nodes[3].id
        descendant_version_before = nodes[3].row_version

        with captured_sql(session) as statements:
            service.move(moved, new_parent_id=new_root.id, expected_row_version=moved_version)

        path_updates = [
            sql
            for sql in statements
            if sql.lstrip().upper().startswith("UPDATE COST_OBJECTS") and "path" in sql
        ]
        assert len(path_updates) == 1, path_updates

        subtree = service.subtree_of(moved).items
        assert [node.code for node in subtree] == ["CHUYEN-1", "CHUYEN-2", "CHUYEN-3"]
        for node in subtree:
            assert node.path.startswith(new_root.path)
            assert node.level == depth_of(node)

        assert service.get(descendant_id).row_version > descendant_version_before


def test_moving_a_node_into_its_own_branch_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        nodes = _chain(service, 3, "VONG")

        with pytest.raises(MasterDataCycleError):
            service.move(
                nodes[0].id,
                new_parent_id=nodes[2].id,
                expected_row_version=nodes[0].row_version,
            )


def test_the_same_code_may_exist_once_shared_and_once_per_branch(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """BR-SYS-01: hai phạm vi, hai ràng buộc duy nhất riêng."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        hanoi = BranchService(session).create(code="MD-HN", name="Hà Nội")
        saigon = BranchService(session).create(code="MD-SG", name="Sài Gòn")

    # Phạm vi chi nhánh phải mang đủ hai chi nhánh: nhật ký của một bản ghi
    # thuộc chi nhánh nào thì mang `branch_id` đó, và RLS trên `audit_log` từ
    # chối dòng ngoài phạm vi người thực hiện. Tạo danh mục riêng cho một chi
    # nhánh mình không được thấy là thao tác **phải** bị chặn.
    scope = _scope(dataset_alpha, (hanoi.id, saigon.id))
    with unit_of_work(session_factory, scope) as session:
        service = MasterDataService(session, ExpenseItem)
        service.create(code="KM001", name="Dùng chung")
        service.create(code="KM001", name="Riêng Hà Nội", branch_id=hanoi.id)
        service.create(code="KM001", name="Riêng Sài Gòn", branch_id=saigon.id)

    with unit_of_work(session_factory, scope) as session:
        service = MasterDataService(session, ExpenseItem)
        assert service.resolve_by_code("KM001").name == "Dùng chung"
        assert service.resolve_by_code("KM001", branch_id=hanoi.id).name == "Riêng Hà Nội"
        # Chi nhánh chưa có bản riêng thì rơi về bản dùng chung.
        assert service.resolve_by_code("KM001", branch_id=999).name == "Dùng chung"


def test_one_branch_never_sees_the_private_catalog_of_another(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Mọi đường đọc lọc theo chi nhánh, không chỉ `resolve_by_code` (sửa sau review M1).

    Bảng danh mục cố ý không bật RLS (`branch_id IS NULL` = dùng chung, mà policy
    chi nhánh sẽ giấu đúng những dòng đó), nên `_visible_to` là lớp lọc **duy
    nhất** — và một đường đọc quên gọi nó thì không có gì phía sau đỡ.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        alpha = BranchService(session).create(code="CHEO-A", name="Chi nhánh A")
        beta = BranchService(session).create(code="CHEO-B", name="Chi nhánh B")

    with unit_of_work(session_factory, _scope(dataset_alpha, (alpha.id, beta.id))) as session:
        service = MasterDataService(session, ExpenseItem)
        group = service.create(code="CHEO-NHOM", name="Nhóm dùng chung", is_group=True)
        service.create(code="CHEO-CHUNG", name="Dùng chung", parent_id=group.id)
        service.create(code="CHEO-RIENG-A", name="Riêng A", parent_id=group.id, branch_id=alpha.id)
        service.create(code="CHEO-RIENG-B", name="Riêng B", parent_id=group.id, branch_id=beta.id)
        group_id = group.id

    with unit_of_work(session_factory, _scope(dataset_alpha, (alpha.id,))) as session:
        service = MasterDataService(session, ExpenseItem)

        seen = {node.code for node in service.children_of(group_id, branch_id=alpha.id).items}
        assert seen == {"CHEO-CHUNG", "CHEO-RIENG-A"}

        in_subtree = {node.code for node in service.subtree_of(group_id, branch_id=alpha.id).items}
        assert "CHEO-RIENG-B" not in in_subtree

        with pytest.raises(MasterDataNotFoundError):
            service.resolve_by_code("CHEO-RIENG-B", branch_id=alpha.id)

        # Không truyền chi nhánh = **chỉ phần dùng chung**, không phải "thấy tất".
        shared_only = {node.code for node in service.children_of(group_id).items}
        assert shared_only == {"CHEO-CHUNG"}


def test_counting_children_ignores_branch_scope(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Đếm con để quyết định cho xóa hay không — phải thấy **cả** nhánh của người khác.

    Cho xóa vì người thực hiện không nhìn thấy nhánh con là cách mất dữ liệu của
    chi nhánh khác mà không ai biết.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        other = BranchService(session).create(code="DEM-KHAC", name="Chi nhánh khác")

    with unit_of_work(session_factory, _scope(dataset_alpha, (other.id,))) as session:
        service = MasterDataService(session, ExpenseItem)
        group = service.create(code="DEM-NHOM", name="Nhóm", is_group=True)
        service.create(
            code="DEM-CON", name="Con của chi nhánh khác", parent_id=group.id, branch_id=other.id
        )
        group_id = group.id

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, ExpenseItem)
        assert service.count_children(group_id) == 1
        with pytest.raises(MasterDataInUseError, match="nhóm con"):
            service.delete(group_id)


def test_two_shared_records_cannot_share_a_code(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chỗ mà `UNIQUE (branch_id, code)` đơn thuần sẽ để lọt: `NULL <> NULL`."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        MasterDataService(session, ExpenseItem).create(code="KM-TRUNG", name="Bản thứ nhất")

    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            MasterDataService(session, ExpenseItem).create(code="KM-TRUNG", name="Bản thứ hai")


def test_renaming_the_code_keeps_the_stable_identifier(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """RT-19: `code` là dữ liệu người dùng sửa được, `uid` thì không."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="UID-CU", name="Trước khi chuẩn hóa mã")
        record_id, original_uid = record.id, record.uid
        # Phải là **v7**, không phải v4: mất tính tăng dần thì chỉ mục `uid`
        # phình theo số bản ghi và không ai nhận ra cho tới khi bảng đủ lớn.
        assert original_uid.version == 7

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        renamed = service.rename(
            record_id,
            expected_row_version=service.get(record_id).row_version,
            code="UID-MOI",
        )
        assert renamed.code == "UID-MOI"
        assert renamed.uid == original_uid


def test_a_record_in_use_cannot_be_deleted(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="DANG-DUNG", name="Đã lên chứng từ")
        record_use(session, entity_type=service.entity_type, entity_id=record.id)
        record_id = record.id

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        assert usage_count_of(session, entity_type=service.entity_type, entity_id=record_id) == 1
        with pytest.raises(MasterDataInUseError) as failure:
            service.delete(record_id)
        assert failure.value.details["usage_count"] == 1


def test_the_usage_counter_accumulates_instead_of_overwriting(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Ba lượt dùng phải thành 3, không phải 1.

    Cộng dồn là toàn bộ điểm của `INSERT … ON CONFLICT DO UPDATE`: một cách cài
    "ghi đè" vẫn cho kết quả đúng ở ca +1 rồi −1 (ca duy nhất mà test cũ đi qua),
    và chỉ sai khi một danh mục được dùng ở nhiều chứng từ — tức là luôn luôn.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="DEM-DON", name="Dùng nhiều lần")
        for _ in range(3):
            record_use(session, entity_type=service.entity_type, entity_id=record.id)

        assert usage_count_of(session, entity_type=service.entity_type, entity_id=record.id) == 3


def test_releasing_a_reference_that_was_never_taken_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Trừ trên bản ghi chưa có dòng đếm phải nổ, không lặng lẽ tạo dòng 0 (H2).

    Ở phase 6+ một đường "gỡ tham chiếu" chạy hai lần sẽ mở khóa xóa cho một
    danh mục đang được dùng — và bộ đếm là thứ duy nhất đứng giữa.

    `ValueError` chứ không `DomainError`: người dùng không gây ra được tình
    huống này từ giao diện và cũng không sửa được nó. Nó là bug của đường ghi,
    và phải nổ to ở log thay vì thành một thông điệp dịu bằng tiếng Việt.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="TRU-HUT", name="Chưa ai dùng")

        with pytest.raises(ValueError, match="chưa có lần dùng nào"):
            record_use(session, entity_type=service.entity_type, entity_id=record.id, delta=-1)


def test_releasing_more_references_than_were_taken_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Vế còn lại do ràng buộc `usage_count >= 0` của DB canh.

    Lượt gỡ thứ hai nằm ở **transaction riêng**: một `IntegrityError` làm hỏng cả
    transaction ở PostgreSQL, nên bắt nó rồi đi tiếp trong cùng khối là cách tự
    dựng ra một `PendingRollbackError` ở chỗ khác.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="TRU-QUA", name="Dùng một, gỡ hai")
        record_use(session, entity_type=service.entity_type, entity_id=record.id)
        record_use(session, entity_type=service.entity_type, entity_id=record.id, delta=-1)
        record_id = record.id

    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            record_use(
                session,
                entity_type=MasterDataService(session, CostObject).entity_type,
                entity_id=record_id,
                delta=-1,
            )


def test_a_record_becomes_deletable_again_once_the_last_use_is_removed(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        record = service.create(code="THA-RA", name="Dùng rồi thôi")
        record_use(session, entity_type=service.entity_type, entity_id=record.id)
        record_use(session, entity_type=service.entity_type, entity_id=record.id, delta=-1)
        record_id = record.id

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        service.delete(record_id)
        with pytest.raises(MasterDataNotFoundError):
            service.get(record_id)


def test_a_group_with_children_cannot_be_deleted(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        nodes = _chain(service, 2, "CO-CON")

        with pytest.raises(MasterDataInUseError, match="nhóm con"):
            service.delete(nodes[0].id)


def test_posting_into_a_group_or_an_inactive_record_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        group = service.create(code="NHOM-HT", name="Nhóm", is_group=True)
        leaf = service.create(code="LA-HT", name="Lá", parent_id=group.id)

        with pytest.raises(MasterDataGroupNotPostableError, match="nút nhóm"):
            service.ensure_postable(group)

        service.ensure_postable(leaf)
        service.set_active(leaf.id, active=False, expected_row_version=leaf.row_version)
        with pytest.raises(MasterDataGroupNotPostableError, match="ngừng theo dõi"):
            service.ensure_postable(leaf)


def test_deactivating_a_group_leaves_its_children_alone(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Không lan xuống con cháu: một thao tác lan ngầm không hoàn tác được."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = MasterDataService(session, CostObject)
        nodes = _chain(service, 3, "KHONG-LAN")
        service.set_active(nodes[0].id, active=False, expected_row_version=nodes[0].row_version)

        assert [node.is_active for node in nodes[1:]] == [True, True]


def test_a_shared_record_cannot_hang_under_a_branch_specific_group(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        branch = BranchService(session).create(code="MD-SCOPE", name="Chi nhánh riêng")

    with unit_of_work(session_factory, _scope(dataset_alpha, (branch.id,))) as session:
        service = MasterDataService(session, CostObject)
        group = service.create(
            code="NHOM-RIENG", name="Nhóm của chi nhánh", is_group=True, branch_id=branch.id
        )

        with pytest.raises(MasterDataParentScopeError):
            service.create(code="CON-CHUNG", name="Dùng chung", parent_id=group.id)


@contextmanager
def captured_sql(session: Session) -> Iterator[list[str]]:
    """Thu lại câu lệnh SQL chạy trong khối — để **đếm** lệnh, không để đọc.

    Cần vì tiêu chí phase 3 nói về *cách* làm chứ không chỉ kết quả: một vòng
    lặp Python cập nhật từng dòng cũng cho ra `path` đúng, và nó sẽ đúng cho tới
    ngày có ai chuyển một nhóm vật tư mười nghìn dòng.
    """
    statements: list[str] = []
    connection = session.connection()

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """Chữ ký do sự kiện `before_cursor_execute` của SQLAlchemy quy định."""
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", record)
