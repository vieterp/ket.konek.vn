"""Bảng nhật ký bất biến — một bản trong **mỗi** schema dataset (FR-NFR-012/013).

Bất biến được ép ở tầng DB bằng tách quyền sở hữu, không phải bằng kỷ luật lập
trình: bảng thuộc `ket_owner`, vai trò runtime `ket_app` chỉ có
`INSERT`/`SELECT` (xem `security/grants.py`). Kể cả khi app server bị chiếm
quyền hoàn toàn, nó vẫn không `UPDATE`/`DELETE`/`TRUNCATE`/`DROP` được nhật ký.

Chỉ ghi **diff các trường đổi**, không ghi cả dòng: `docs/srs/01` §13 Q3 nêu
đúng lo ngại phình dung lượng, và một bảng nhật ký to gấp mười lần dữ liệu gốc
sẽ bị người quản trị xóa — tức là mất luôn thứ đang cố bảo vệ.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.types import AuditValues


class AuditAction(StrEnum):
    """Hành vi được ghi vết.

    Danh sách này là **hợp đồng** với màn hình tra cứu nhật ký và với ràng buộc
    `CHECK` dưới DB — thêm giá trị mới phải kèm migration.
    """

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    POSTED = "posted"
    UNPOSTED = "unposted"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    PRINTED = "printed"


AUDIT_TABLE_NAME = "audit_log"


class AuditLog(DatasetBase):
    """Một dòng nhật ký. Chỉ thêm, không bao giờ sửa."""

    __tablename__ = AUDIT_TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{action.value}'" for action in AuditAction) + ")",
            name="action_known",
        ),
        # Tra cứu "lịch sử của chứng từ này" — truy vấn hay dùng nhất khi có
        # tranh chấp số liệu, nên nó quyết định hình dạng index.
        Index("ix_audit_log_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_user", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """Người thực hiện. Không có khóa ngoại tới `public.users` — dataset phải
    dump/restore độc lập được (xem `persistence/base.py`)."""

    branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """NULL = sự kiện mức hệ thống, không thuộc chi nhánh nào. Policy RLS cho
    NULL đi qua, nếu không thì sự kiện đăng nhập/đổi cấu hình vô hình với mọi
    người."""

    entity_type: Mapped[str] = mapped_column(String(150), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    """Khóa chính dạng chuỗi; khóa phức hợp nối bằng `/` — nhật ký phải ghi
    được **mọi** bảng nên không thể dùng kiểu khóa của riêng bảng nào.

    Rộng rãi có chủ đích: từ phase 4, khóa phức hợp của bảng phát sinh và bảng
    số dư gồm nhiều thành phần (kỳ, sổ, chi nhánh, tài khoản, tiền tệ). Nới một
    cột `varchar` trên bảng chỉ-thêm đã có hàng triệu dòng là việc phải khóa
    bảng; nới bây giờ thì miễn phí."""

    action: Mapped[str] = mapped_column(String(20), nullable=False)

    old_values: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)
    new_values: Mapped[AuditValues | None] = mapped_column(JSONB, nullable=True)

    correlation_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    """Nối các dòng nhật ký của cùng một request — thứ duy nhất cho phép dựng
    lại "người dùng bấm một nút thì hệ thống đổi những gì"."""

    client_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
