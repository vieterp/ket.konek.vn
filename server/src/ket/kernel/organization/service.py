"""Cơ cấu tổ chức: cây chi nhánh (FR-SYS-050, FR-NFR-030).

Bảng `branches` nằm ở `kernel/security/models.py` chứ không ở đây, có chủ đích:
nó ra đời ở phase 2 làm **neo cô lập dữ liệu** cho RLS, và mọi thứ trong luồng
đăng nhập đã trỏ vào đó. Chuyển bảng sang module khác chỉ để cho gọn thư mục sẽ
động vào đường xác thực — thay đổi rủi ro nhất trong hệ — để đổi lấy tính thẩm
mỹ. Cái thuộc về phase 3 là **cây** và các thao tác trên cây, và chúng ở đây.

Chi nhánh khác danh mục thường ở hai điểm, nên nó không dùng
`MasterDataService`:

* Không có phạm vi "dùng chung / riêng chi nhánh" — bảng này *là* chi nhánh.
* Xóa một chi nhánh không phải việc của bộ đếm `master_data_usage` mà của RLS:
  chi nhánh có dữ liệu là chi nhánh mà **mọi** bảng phát sinh đang trỏ tới, và
  khóa ngoại `RESTRICT` là thứ chặn thật.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    BranchNotFoundError,
    MasterDataCycleError,
)
from ket.kernel.master_data.tree_path import (
    ROOT_LEVEL,
    child_path,
    is_descendant_of,
    like_prefix,
    move_subtree_params,
    move_subtree_sql,
)
from ket.kernel.persistence.sequences import reserve_id
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.security.models import Branch

BRANCH_TABLE_NAME = "branches"


class BranchService:
    """Thao tác cây trên danh mục chi nhánh, trong transaction đang mở."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, branch_id: int) -> Branch:
        branch = self._session.get(Branch, branch_id)
        if branch is None:
            raise BranchNotFoundError("Không tìm thấy chi nhánh", branch_id=branch_id)
        return branch

    def create(
        self,
        *,
        code: str,
        name: str,
        name_en: str | None = None,
        parent_id: int | None = None,
        is_dependent_accounting: bool = True,
        tax_code: str | None = None,
        address: str | None = None,
    ) -> Branch:
        """Thêm chi nhánh, tự dựng `path`/`level` từ chi nhánh cha.

        Lấy khóa chính trước từ sequence vì `path` chứa chính khóa đó — cùng lý
        do đã ghi ở `MasterDataService.create`.
        """
        parent = self.get(parent_id) if parent_id is not None else None
        branch_id = reserve_id(self._session, BRANCH_TABLE_NAME)

        branch = Branch(
            id=branch_id,
            code=code,
            name=name,
            name_en=name_en,
            parent_id=parent_id,
            path=child_path(parent.path if parent else None, branch_id),
            level=(parent.level + 1) if parent else ROOT_LEVEL,
            is_dependent_accounting=is_dependent_accounting,
            tax_code=tax_code,
            address=address,
        )
        self._session.add(branch)
        self._session.flush()
        return branch

    def move(self, branch_id: int, *, new_parent_id: int | None, expected_row_version: int) -> None:
        """Chuyển chi nhánh (và cấp dưới của nó) sang trực thuộc đơn vị khác."""
        branch = self.get(branch_id)
        require_row_version(
            current=branch.row_version,
            expected=expected_row_version,
            entity=BRANCH_TABLE_NAME,
        )
        new_parent = self.get(new_parent_id) if new_parent_id is not None else None
        if new_parent is not None and is_descendant_of(new_parent.path, branch.path):
            raise MasterDataCycleError(
                "Không chuyển được một đơn vị vào bên trong chính cấp dưới của nó",
                entity_type=BRANCH_TABLE_NAME,
                entity_id=branch_id,
                new_parent_id=new_parent_id,
            )

        old_path = branch.path
        new_path = child_path(new_parent.path if new_parent else None, branch_id)
        if new_path == old_path:
            return

        branch.parent_id = new_parent_id
        self._session.flush()
        statement = move_subtree_sql(BRANCH_TABLE_NAME)
        self._session.execute(
            text(statement), move_subtree_params(old_path=old_path, new_path=new_path)
        )
        self._session.expire_all()

    def subtree_of(self, branch_id: int) -> Sequence[Branch]:
        """Chi nhánh đó và mọi cấp dưới — dùng để dựng phạm vi báo cáo hợp nhất.

        Phạm vi RLS của một người dùng vẫn là danh sách phẳng lấy từ
        `user_branches` (lát 2B-1b), **không** tự nới theo cây: được xem trụ sở
        không có nghĩa là được xem lương của mọi chi nhánh con, và một phép nới
        ngầm theo cây sẽ biến mỗi lần thêm chi nhánh thành một lần mở rộng quyền
        mà không ai bấm nút nào.
        """
        branch = self.get(branch_id)
        return (
            self._session.execute(
                select(Branch)
                .where(Branch.path.like(like_prefix(branch.path)))
                .order_by(Branch.path)
            )
            .scalars()
            .all()
        )
