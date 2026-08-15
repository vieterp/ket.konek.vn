"""Bảng phân quyền và tổ chức — **trong từng schema dataset**.

Danh tính (`users`) là toàn cục, còn **quyền** thì thuộc về từng dữ liệu kế
toán: cùng một người có thể là kế toán trưởng ở doanh nghiệp này và chỉ được
xem ở doanh nghiệp kia. Vì vậy `user_roles`/`user_branches` nằm ở đây chứ không
ở schema điều khiển.

Mã quyền theo dạng `{module}.{document_type}.{action}` (FR-SYS-071) — sinh từ
registry loại chứng từ, nên thêm loại chứng từ mới **không** phải sửa bảng
quyền. Registry và dependency `require_permission` thuộc bước 8 (slice sau);
bảng phải có từ bây giờ vì migration `0001` là nơi RLS được bật.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned


class Branch(DatasetBase, Audited, RowVersioned):
    """Chi nhánh / đơn vị trực thuộc (LD-12, FR-SYS-072).

    Đồng thời là **neo cô lập dữ liệu**: mọi bảng nghiệp vụ mang `branch_id` và
    policy RLS so nó với phạm vi của request. Phase 3 mở rộng bảng này (địa
    chỉ, mã số thuế, thông tin in trên chứng từ); ở đây chỉ có phần mà cơ chế
    phân quyền cần.

    Bản thân bảng này **không** bật RLS — xem lý do trong migration `0001`
    (`_apply_row_level_security`): policy trên danh mục sẽ chặn luôn việc tạo
    chi nhánh mới. Quyền xem/sửa danh mục thuộc về RBAC.

    `RowVersioned` vì đây là danh mục **người dùng sửa qua form**: hai người
    cùng mở một chi nhánh rồi lần lượt lưu là chuyện bình thường, và người lưu
    sau không được ghi đè im lặng thay đổi của người lưu trước (FR-NFR-005).
    """

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Role(DatasetBase, Audited):
    """Vai trò gộp quyền. `is_system` = do gói cấu hình tạo, người dùng không xóa được."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Permission(DatasetBase, Audited):
    """Một mã quyền `{module}.{document_type}.{action}`."""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)


class RolePermission(DatasetBase, Audited):
    """Gán quyền cho vai trò.

    `allow` cho phép ghi **cấm tường minh**: một vai trò kế thừa quyền rộng vẫn
    chặn được đúng một hành vi mà không phải tách vai trò mới. Quy tắc phân xử
    (cấm thắng cho phép) thuộc bước 8.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )
    allow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class UserRole(DatasetBase, Audited):
    """Vai trò của một người dùng **trong dữ liệu kế toán này**."""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class UserBranch(DatasetBase, Audited):
    """Chi nhánh mà một người dùng được phép chạm (FR-SYS-072).

    Bảng này **không** bật RLS, có chủ đích: nó chính là nguồn để dựng phạm vi
    RLS lúc đăng nhập. Bật RLS lên nó tạo vòng lặp — muốn biết được xem chi
    nhánh nào thì đã phải biết được xem chi nhánh nào.
    """

    __tablename__ = "user_branches"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True
    )


class Setting(DatasetBase, Audited, RowVersioned):
    """Tùy chọn hai cấp: chung hệ thống hoặc riêng người dùng (FR-SYS-060, BR-SYS-06).

    Giá trị lưu chuỗi + `value_type` thay vì JSONB: tùy chọn ở đây là vô hướng
    (số chữ số thập phân, phím Enter/Tab, ngôn ngữ) và chuỗi có kiểu thì so
    sánh, index và đọc bằng mắt trong bản dump đều dễ hơn.

    `RowVersioned` vì tùy chọn **chung hệ thống** là bản ghi dùng chung: hai
    quản trị viên cùng chỉnh màn hình thiết lập thì người lưu sau phải biết bản
    mình đang xem đã cũ — `money.scale` bị ghi đè im lặng sẽ đổi cách làm tròn
    của toàn bộ dữ liệu kế toán.
    """

    __tablename__ = "settings"
    __table_args__ = (
        # `UNIQUE (scope, user_id, key)` KHÔNG dùng được: PostgreSQL coi mọi
        # NULL là khác nhau, nên hai dòng cấu hình chung cùng khóa vẫn lọt và
        # `settings_service` sẽ đọc trúng dòng nào tùy thứ tự — lỗi không tái
        # hiện được. Hai index bộ phận khóa đúng bất biến cho từng cấp.
        Index(
            "uq_settings_system_key", "key", unique=True, postgresql_where=text("scope = 'system'")
        ),
        Index(
            "uq_settings_user_key",
            "user_id",
            "key",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        CheckConstraint(
            "(scope = 'system' AND user_id IS NULL) OR (scope = 'user' AND user_id IS NOT NULL)",
            name="scope_user_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(String(1000), nullable=False)
    value_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="string", server_default="string"
    )
