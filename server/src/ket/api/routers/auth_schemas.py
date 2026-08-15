"""Hợp đồng JSON của nhóm `/auth` — Pydantic ở mọi ranh giới API (ADR-015).

`SecretStr` cho mọi trường mật khẩu: `repr()` của model đi vào log lỗi, vào
traceback và vào phần hiển thị của trình gỡ rối, và `SecretStr` in ra
`**********` ở cả ba chỗ. Không phải lớp bảo vệ mạnh, nhưng nó chặn đúng đường
mà mật khẩu hay rò nhất — rò vào log của chính mình.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, SecretStr


class LoginRequest(BaseModel):
    """Đăng nhập. `totp_code` chỉ cần cho tài khoản bắt buộc 2FA.

    Client bình thường gửi lần đầu **không** kèm mã; nhận `auth.totp_required`
    thì hỏi mã rồi gửi lại. Nhờ vậy màn hình đăng nhập không phải hỏi mã 2FA của
    những người không dùng 2FA.
    """

    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=200)
    totp_code: str | None = Field(default=None, max_length=16)


class LoginResponse(BaseModel):
    """Phiên vừa cấp. `token` chỉ xuất hiện ở đây, không bao giờ đọc lại được."""

    token: str
    expires_at: datetime
    must_change_password: bool
    """`True` → client phải đưa thẳng người dùng tới màn hình đổi mật khẩu.
    Ép ở server (chặn các endpoint khác) là việc của lát 2B-1b, khi đã có
    `require_permission` để gắn ngoại lệ vào."""


class MeResponse(BaseModel):
    """Danh tính của phiên hiện tại. Chưa có vai trò/quyền — chúng per-dataset."""

    user_id: int
    username: str
    locale: str
    must_change_password: bool
    expires_at: datetime


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=200)
    new_password: SecretStr = Field(min_length=1, max_length=200)


class TotpEnrollRequest(BaseModel):
    """Bắt đầu đăng ký 2FA — phải nhập lại mật khẩu.

    Đã có phiên hợp lệ vẫn phải chứng minh lại: gắn thiết bị sinh mã là thao tác
    quyết định ai giữ được tài khoản về sau, và một máy trạm bỏ quên không khóa
    màn hình không được phép làm việc đó.
    """

    password: SecretStr = Field(min_length=1, max_length=200)


class TotpEnrollResponse(BaseModel):
    """URI `otpauth://` để client dựng mã QR.

    **Chứa bí mật dạng rõ.** Chỉ trả đúng một lần cho chính người đang đăng ký;
    không ghi log, không ghi nhật ký, không lưu lại ở client.
    """

    provisioning_uri: str


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)
