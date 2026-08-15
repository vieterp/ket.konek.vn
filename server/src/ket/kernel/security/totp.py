"""Xác thực hai lớp bằng mã một lần theo thời gian — TOTP RFC 6238 (FR-NFR-016).

Bắt buộc cho vai trò quản trị và quyền `bank.*` / `einvoice.*` (FR-SYS-074): đó
là những quyền chuyển được tiền ra khỏi doanh nghiệp và phát hành được hóa đơn
mang tên doanh nghiệp.

**Điểm ép nằm ở cờ toàn cục `users.totp_required`, kiểm lúc đăng nhập.** Vai trò
là thứ per-dataset, còn đăng nhập xảy ra **trước** khi chọn dữ liệu kế toán —
nên câu hỏi "người này có vai trò nhạy cảm không" phải được trả lời sẵn ở tầng
danh tính. Lát sau (2B-1b) bật cờ này khi gán vai trò nhạy cảm ở bất kỳ dataset
nào; ở đây chỉ có phần **ép**.

Chống phát lại (`last_counter`) là phần mà thư viện không làm hộ, xem
`verify_code`.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Final

import pyotp

from ket.kernel.errors import InvalidTotpCodeError, TotpCodeReusedError

PERIOD_SECONDS: Final[int] = 30
DIGITS: Final[int] = 6
ISSUER: Final[str] = "Konek Két"

ACCEPT_WINDOW: Final[int] = 1
"""Chấp nhận thêm một bước 30 giây về mỗi phía — tổng cửa sổ ~90 giây.

Cần thiết vì đồng hồ máy trạm trong LAN thường lệch vài chục giây (không phải
máy nào cũng đồng bộ NTP), và người dùng gõ xong mã thì bước đã trôi. Rộng hơn
nữa thì cửa sổ dùng lại một mã bị đánh cắp cũng rộng theo."""


def generate_secret() -> str:
    """Bí mật base32 mới cho một lần đăng ký thiết bị."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, username: str) -> str:
    """URI `otpauth://` để dựng mã QR cho Google Authenticator/Authy.

    Chuỗi này **chứa bí mật** — chỉ trả đúng một lần, ngay lúc đăng ký, cho
    chính người đang đăng ký. Không ghi vào log, không ghi vào nhật ký.
    """
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD_SECONDS).provisioning_uri(
        name=username, issuer_name=ISSUER
    )


def verify_code(secret: str, code: str, *, last_counter: int | None, now: datetime) -> int:
    """Kiểm mã và trả về **bước thời gian** đã khớp, để người gọi lưu lại.

    Không dùng `TOTP.verify(..., valid_window=1)` của thư viện vì nó chỉ trả
    `True`/`False` — mất thông tin *bước nào* đã khớp, mà đó chính là thứ cần để
    chặn dùng lại. Vòng lặp dưới đây làm cùng việc và giữ lại con số đó.

    So sánh bằng `compare_digest`: so bằng `==` trên chuỗi thoát ra ở ký tự khác
    đầu tiên, và sáu chữ số là đủ ngắn để chênh lệch thời gian nói lên vài chữ
    số đầu.
    """
    cleaned = code.strip().replace(" ", "")
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD_SECONDS)
    current = int(now.timestamp()) // PERIOD_SECONDS

    for counter in range(current - ACCEPT_WINDOW, current + ACCEPT_WINDOW + 1):
        expected = totp.at(counter * PERIOD_SECONDS)
        if not secrets.compare_digest(expected, cleaned):
            continue
        if last_counter is not None and counter <= last_counter:
            raise TotpCodeReusedError(
                "Mã xác thực này đã được dùng — chờ mã kế tiếp trên ứng dụng sinh mã"
            )
        return counter

    raise InvalidTotpCodeError("Mã xác thực hai lớp không đúng hoặc đã hết hiệu lực")
