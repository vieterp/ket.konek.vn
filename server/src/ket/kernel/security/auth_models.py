"""Phiên đăng nhập — bảng của **schema điều khiển** (`public`).

Không cùng tệp với `security/models.py`: tệp đó chứa bảng phân quyền, và phân
quyền là thứ **per-dataset** (cùng một người là kế toán trưởng ở doanh nghiệp
này, chỉ được xem ở doanh nghiệp kia). Phiên thì gắn với **danh tính**, tức là
toàn cục, tức là schema điều khiển. Trộn hai loại vào một tệp là cách nhanh
nhất để ai đó đặt nhầm bảng tiếp theo vào sai schema.

**Token đối xứng lưu DB** thay vì JWT tự chứa (plan.md bước 7): trong LAN, thu
hồi phải có hiệu lực **ngay** — đuổi một nhân viên vừa nghỉ việc ra khỏi hệ
thống không thể phụ thuộc vào việc chờ token hết hạn. JWT muốn thu hồi ngay thì
cũng phải tra DB mỗi request, tức là mất đúng ưu điểm duy nhất của nó.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, DateTime, Index, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import ControlBase

SESSION_TABLE_NAME = "auth_sessions"

TOKEN_BYTES: Final[int] = 32
"""256 bit entropy. Token là bí mật mang toàn quyền của một người dùng trong
suốt phiên, nên nó phải nằm ngoài tầm dò kể cả khi ai đó thử liên tục trong LAN."""


def new_session_token() -> str:
    """Token ngẫu nhiên an toàn mật mã, dạng chuỗi URL-safe."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> bytes:
    """SHA-256 của token — **thứ duy nhất** được ghi xuống DB.

    Không băm chậm (Argon2) như mật khẩu, và có lý do: token đã là 256 bit ngẫu
    nhiên nên không có "từ điển" nào để dò: băm chậm chỉ thêm chi phí cho mỗi
    request mà không thêm chút an toàn nào.

    Băm là bắt buộc dù vậy: một bản dump DB hay một lần đọc trộm bảng không được
    biến thành quyền đăng nhập dưới danh nghĩa người khác.

    Mã hóa **UTF-8, không phải ASCII**, dù token do hàm trên sinh ra luôn là
    ASCII: chuỗi vào đây đến từ header `Authorization` của người gọi, tức là dữ
    liệu ngoài. `ascii` sẽ ném `UnicodeEncodeError` cho một token có dấu — biến
    một câu trả lời 401 bình thường thành lỗi 500, và cho người dò một cách phân
    biệt hai nhánh.
    """
    return hashlib.sha256(token.encode("utf-8")).digest()


class AuthSession(ControlBase):
    """Một phiên đăng nhập đang sống hoặc đã kết thúc."""

    __tablename__ = SESSION_TABLE_NAME
    __table_args__ = (Index("ix_auth_sessions_user", "user_id", "expires_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    """`bytea` chứ không `text`: cùng lý do với `users.totp_secret_enc` — kiểu
    cột chặn việc ai đó lỡ tay ghi token dạng rõ vào đây."""

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    """Cập nhật **có tiết chế** (xem `auth_service.LAST_SEEN_REFRESH`): ghi mỗi
    request sẽ biến mọi lần đọc thành một lần ghi, và một bảng phiên nóng lên vô
    ích trong khi thông tin thu được chỉ dùng cho màn hình quản trị phiên."""

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Thu hồi bằng cách đánh dấu chứ không xóa dòng: "phiên này bị thu hồi lúc
    nào, còn sống bao lâu" là câu hỏi của điều tra sự cố, và một dòng bị xóa thì
    không trả lời được."""

    client_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
