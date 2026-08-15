"""Bảng của schema điều khiển (`public`) — ADR-017.

Ba bảng, và chỉ ba bảng, sống ngoài schema dataset:

* `datasets` — sổ đăng ký dữ liệu kế toán: mã, schema, chế độ TT200/TT133.
* `users` — danh tính đăng nhập **toàn cục**: một người dùng một mật khẩu, mở
  được nhiều dữ liệu kế toán. Quyền thì per-dataset (`user_roles`,
  `user_branches` nằm trong schema dataset) vì vai trò của một người ở mỗi
  doanh nghiệp là khác nhau.
* `system_metadata` — phiên bản schema điều khiển + thông tin cho handshake.

Mọi thứ còn lại thuộc về dataset. Ranh giới này là thứ khiến "sao lưu/khôi
phục một doanh nghiệp" (RT-03) trở thành thao tác trên **một** schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import ControlBase


class Dataset(ControlBase):
    """Một dữ liệu kế toán = một schema PostgreSQL (FR-SYS-001)."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    schema_name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    scheme: Mapped[str] = mapped_column(String(10), nullable=False)
    """`TT200` hoặc `TT133` — chế độ kế toán áp dụng. Là **dữ liệu**, không phải
    nhánh code (LD-06): đổi chế độ = kích hoạt gói cấu hình khác ở phase 5."""

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(ControlBase):
    """Danh tính đăng nhập toàn cục (FR-SYS-070, FR-NFR-010).

    Phase 2 slice này chỉ dựng **bảng** — băm mật khẩu Argon2id, chính sách mật
    khẩu, TOTP và đặt lại mật khẩu thuộc bước 7 (slice sau). Bảng có mặt từ bây
    giờ vì `audit_log.user_id` và `user_roles.user_id` trỏ tới nó về mặt nghiệp
    vụ (không phải bằng khóa ngoại — xem `persistence/base.py`).
    """

    __tablename__ = "users"
    __audit_exclude__ = frozenset({"password_hash", "totp_secret_enc"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    """Bí mật TOTP **đã mã hóa** bằng khóa app lấy từ OS keystore (RT-05,
    ADR-019). Cột `bytea` chứ không phải `text` để không ai lỡ tay ghi giá trị
    dạng rõ vào đây; bản dump và bản sao lưu vì thế cũng không chứa bí mật."""

    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(
        String(5), nullable=False, default="vi", server_default="vi"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemMetadata(ControlBase):
    """Cặp khóa–giá trị mức cụm: phiên bản schema điều khiển, phiên bản server."""

    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


CONTROL_SCHEMA_VERSION_KEY = "control_schema_version"
