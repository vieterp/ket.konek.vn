"""Nhật ký của schema điều khiển — sự kiện tài khoản và phiên (quyết định D1).

Vì sao **không** ghi vào `audit_log` của dataset: đăng nhập, đổi mật khẩu, đăng
ký 2FA xảy ra **trước khi** người dùng chọn dữ liệu kế toán, nên lúc đó không có
schema dataset nào để ghi vào. Ghi vào một dataset tùy chọn (dataset đầu tiên,
dataset mặc định) sẽ làm vết đăng nhập của cả doanh nghiệp biến mất khi dataset
đó được khôi phục hoặc xóa.

Cùng khuôn bất biến với `audit_log` (RT-02): bảng thuộc `ket_owner`, vai trò
runtime chỉ `INSERT`/`SELECT`. Xem `security/grants.grant_append_only` — dùng
lại đúng hàm đó, không chép luật lần hai.

Khác `audit_log` ở một điểm: **không có listener**. `User`/`AuthSession` không
kế thừa `Audited` và không nên kế thừa — listener chỉ biết `DatasetBase`. Ghi
vết ở đây là lời gọi tường minh `record_control_action` trong tầng dịch vụ, và
nó nằm trong **cùng transaction** với thay đổi mà nó mô tả.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from ket.kernel.persistence.base import ControlBase
from ket.kernel.persistence.types import AuditValues


class ControlAuditAction(StrEnum):
    """Sự kiện mức tài khoản. Hợp đồng với ràng buộc `CHECK` dưới DB."""

    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    # S105 báo nhầm: đây là *tên hành vi* ghi vào nhật ký, không phải mật khẩu.
    PASSWORD_CHANGED = "password_changed"  # noqa: S105
    PASSWORD_RESET = "password_reset"  # noqa: S105
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_BLOCKED = "login_blocked"
    LOGOUT = "logout"
    SESSION_REVOKED = "session_revoked"
    TOTP_ENROLLED = "totp_enrolled"
    TOTP_DISABLED = "totp_disabled"

    SESSIONS_PRUNED = "sessions_pruned"
    """Dọn phiên đã kết thúc (`ket.admin prune-sessions`).

    Bảng `auth_sessions` là nguồn trả lời "tài khoản đó đăng nhập từ máy nào,
    lúc mấy giờ" khi có sự cố — nên việc **xóa** nó phải tự nó là một sự kiện
    kiểm toán. Không có dòng này thì lịch sử phiên biến mất mà không ai biết ai
    đã cho lệnh, lúc nào, với mốc lưu trữ nào."""


CONTROL_AUDIT_TABLE_NAME = "control_audit_log"


class ControlAuditLog(ControlBase):
    """Một dòng nhật ký điều khiển. Chỉ thêm, không bao giờ sửa."""

    __tablename__ = CONTROL_AUDIT_TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{action.value}'" for action in ControlAuditAction) + ")",
            name="action_known",
        ),
        Index("ix_control_audit_log_occurred_at", "occurred_at"),
        Index("ix_control_audit_log_subject", "subject_user_id", "occurred_at"),
        Index("ix_control_audit_log_action", "action", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Người thực hiện. `NULL` khi chưa xác định được danh tính — đăng nhập
    thất bại, hoặc thao tác chạy từ CLI của người quản trị máy chủ."""

    subject_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Người bị tác động. Bằng `actor_user_id` với thao tác tự phục vụ (đổi mật
    khẩu của chính mình), khác khi quản trị viên đặt lại mật khẩu cho người
    khác."""

    subject_label: Mapped[str | None] = mapped_column(String(150), nullable=True)
    """Tên đăng nhập **được nhập vào**, giữ nguyên kể cả khi không có tài khoản
    nào khớp.

    Đây là cột làm nên giá trị điều tra của bảng: một chuỗi `login_failed` với
    những `subject_label` khác nhau là dò tài khoản, còn cùng một
    `subject_label` là một người quên mật khẩu."""

    action: Mapped[str] = mapped_column(String(40), nullable=False)

    old_values: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)
    new_values: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)
    """**Không bao giờ** chứa mật khẩu, hash, bí mật TOTP hay token phiên — chỉ
    các cột trạng thái (`is_active`, `totp_required`, `locked_until`). Nhật ký
    không được trở thành chỗ rò bí mật thứ hai (RT-05)."""

    correlation_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    client_info: Mapped[str | None] = mapped_column(String(255), nullable=True)


def record_control_action(
    session: Session,
    *,
    action: ControlAuditAction,
    actor_user_id: int | None = None,
    subject_user_id: int | None = None,
    subject_label: str | None = None,
    old_values: AuditValues | None = None,
    new_values: AuditValues | None = None,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> None:
    """Ghi một sự kiện tài khoản vào **cùng transaction** với thay đổi của nó.

    Cùng transaction là điều kiện để hai bất biến đứng vững: không có vết cho
    một thay đổi bị rollback, và không có thay đổi nào lọt qua mà không có vết.

    Riêng đường **đăng nhập thất bại** phải commit dù kết quả là lỗi — xem
    `auth_service.authenticate`: nó dựng lỗi rồi mới ném **sau khi** transaction
    đã commit, chứ không ném từ trong lòng transaction.
    """
    session.add(
        ControlAuditLog(
            action=action.value,
            actor_user_id=actor_user_id,
            subject_user_id=subject_user_id,
            subject_label=subject_label,
            old_values=old_values,
            new_values=new_values,
            correlation_id=correlation_id,
            client_info=client_info,
        )
    )
