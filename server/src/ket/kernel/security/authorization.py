"""Phân giải "người này được làm gì, ở dữ liệu kế toán nào" (FR-SYS-071/072).

Đây là nửa còn lại của bộ đôi với `auth_service`: ở đó trả lời **ai đăng nhập**
(toàn cục, schema điều khiển), ở đây trả lời **được làm gì** (per-dataset, chạy
sau khi transaction đã `SET ROLE` + `search_path` vào schema của dataset).

Hai quyết định định hình tệp này:

1. **Cấm thắng cho phép.** `role_permissions.allow = false` là một lệnh cấm
   tường minh, và nó thắng mọi vai trò khác cấp cùng mã quyền. Nhờ vậy "kế toán
   viên nhưng không được xóa chứng từ" là một vai trò cấm thêm chứ không phải
   một bản sao của vai trò gốc thiếu vài dòng — bản sao là thứ sẽ trôi khỏi bản
   gốc ở lần sửa quyền tiếp theo.
2. **Không có vai trò = không thuộc doanh nghiệp này.** Trả tập quyền rỗng cũng
   đúng về mặt kỹ thuật, nhưng nó khiến mọi màn hình báo "thiếu quyền" thay vì
   nói thẳng rằng người này không có phần trong dữ liệu kế toán đang mở.

Phạm vi chi nhánh (`user_branches`) đọc cùng lượt: nó là **đầu vào** của GUC
RLS, nên nó phải có mặt trước câu truy vấn nghiệp vụ đầu tiên.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import DatasetAccessDeniedError, PermissionDeniedError
from ket.kernel.security.models import Permission, RolePermission, UserBranch, UserRole


@dataclass(frozen=True)
class Access:
    """Quyền hiệu lực của một người dùng trong **một** dữ liệu kế toán."""

    user_id: int
    role_ids: tuple[int, ...]
    permissions: frozenset[str]
    branch_ids: tuple[int, ...]

    def has(self, code: str) -> bool:
        """Có quyền này không, **không** ném lỗi.

        Dùng cho chỗ lọc danh sách (màn hình chỉ hiện việc người dùng bấm được)
        chứ không phải chỗ chặn. Hai hàm riêng vì hai hướng mặc định khác nhau:
        `require` chặn, `has` chỉ trả lời — và dùng `has` ở chỗ đáng lẽ phải
        chặn là kiểu lỗi mà `require` sinh ra để không xảy ra.
        """
        return code in self.permissions

    def require(self, code: str) -> None:
        """Chặn khi thiếu quyền. `details.permission` nêu đúng mã còn thiếu."""
        if code not in self.permissions:
            raise PermissionDeniedError(
                "Tài khoản không có quyền thực hiện thao tác này", permission=code
            )


def resolve_access(session: Session, *, user_id: int) -> Access:
    """Đọc vai trò, quyền và chi nhánh của một người dùng trong dataset hiện hành.

    Gọi trong transaction **đã định tuyến** (`persistence.session.dataset_session`
    hoặc `unit_of_work`): các bảng đọc ở đây nằm trong schema dataset, và quyền
    đọc chúng thuộc về `ds_<mã>_app` chứ không phải vai trò đăng nhập.

    Bốn bảng này cố ý **không** bật RLS (xem migration `0001`), nên hàm chạy
    được khi phạm vi chi nhánh còn rỗng — và phải như vậy, vì chính nó là thứ
    tính ra phạm vi đó.
    """
    role_ids = tuple(
        session.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id)).all()
    )
    if not role_ids:
        raise DatasetAccessDeniedError(
            "Tài khoản chưa được gán vai trò nào trong dữ liệu kế toán này"
        )

    granted: set[str] = set()
    denied: set[str] = set()
    rows = session.execute(
        select(Permission.code, RolePermission.allow)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(role_ids))
    ).all()
    for code, allow in rows:
        (granted if allow else denied).add(code)

    branch_ids = tuple(
        sorted(
            session.scalars(select(UserBranch.branch_id).where(UserBranch.user_id == user_id)).all()
        )
    )

    return Access(
        user_id=user_id,
        role_ids=tuple(sorted(role_ids)),
        # Hiệu số chứ không phải "ai ghi sau thắng": thứ tự đọc từ DB không có
        # nghĩa nghiệp vụ nào, nên để nó quyết định quyền là để quyền phụ thuộc
        # vào kế hoạch truy vấn.
        permissions=frozenset(granted - denied),
        branch_ids=branch_ids,
    )
