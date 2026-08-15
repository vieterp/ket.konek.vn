"""Khóa mã hóa của ứng dụng: lấy từ OS keystore, không bao giờ từ DB (ADR-019, RT-05).

Bí mật mà hệ này giữ — `totp_secret`, token eSign ghi nhớ, mật khẩu kết nối DB —
không được nằm dạng rõ trong PostgreSQL, vì **bản sao lưu mang theo mọi thứ
PostgreSQL có**. Một file `pg_dump` để trên ổ chia sẻ trong LAN là đường rò dữ
liệu thật, không phải giả định.

Vì vậy khóa sống ở nơi PostgreSQL không với tới:

* **macOS** — Keychain, qua `keyring`.
* **Windows** — Credential Manager (DPAPI đứng sau), qua `keyring`.
* **Dev/CI** — biến môi trường `KET_APP_KEY`, đặt tường minh trong cấu hình.

Ba điều cố ý **không** làm:

1. **Không tự sinh khóa khi thiếu.** Sinh ngầm nghĩa là mỗi lần cài lại sẽ có
   một khóa mới, và mọi ciphertext cũ trở thành rác không đọc được — im lặng.
   Sinh khóa là một lệnh tường minh (`ket.admin generate-app-key`).
2. **Không đọc khóa lúc khởi động rồi giữ suốt đời tiến trình một cách bắt
   buộc.** App server phải khởi động được trên máy chưa cấu hình keystore; chỉ
   thao tác **chạm bí mật** mới hỏng, với thông điệp chỉ đúng cách sửa.
3. **Không tự chuyển sang lưu dạng rõ khi thiếu khóa.** Fail-closed.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ket.kernel.errors import AppKeyUnavailableError

KEYRING_USERNAME = "app_key"
"""Tên mục trong keystore. Dịch vụ (`service`) lấy từ `Settings.keyring_service`
để hai bản cài trên cùng một máy không giẫm lên khóa của nhau."""


def generate_app_key() -> str:
    """Sinh khóa mới dạng chuỗi base64 mà `Fernet` nhận."""
    return Fernet.generate_key().decode("ascii")


def store_app_key(key: str, *, service: str) -> None:
    """Ghi khóa vào OS keystore. Ghi đè khóa cũ nếu có.

    Không kiểm "đã có khóa chưa" ở đây: người gọi (`ket.admin`) mới biết được
    ghi đè là chủ đích hay tai nạn, và chỉ nó mới hỏi lại được người dùng.
    """
    import keyring  # nạp trễ: xem `_read_from_keystore`

    keyring.set_password(service, KEYRING_USERNAME, key)


def _read_from_keystore(service: str) -> str | None:
    """Đọc khóa từ OS keystore; trả `None` khi máy không có keystore dùng được.

    `keyring` được import **trong hàm** chứ không ở đầu tệp: trên Linux không có
    D-Bus (container CI, dịch vụ chạy nền), việc dò backend xảy ra ngay lúc
    import và tốn thời gian cho một đường mà bản cài đó không bao giờ đi.

    Mọi lỗi của keystore quy về `None` — "không có khóa dùng được" — chứ không
    nổi lên dạng ngoại lệ riêng của thư viện: người gọi chỉ có đúng một cách xử
    lý, và `keyring` đổi cây ngoại lệ giữa các bản.
    """
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - keyring là phụ thuộc bắt buộc
        return None
    try:
        return keyring.get_password(service, KEYRING_USERNAME)
    except KeyringError:
        return None


def load_app_key(*, service: str, override: str | None = None) -> bytes:
    """Khóa app dạng bytes cho `Fernet`. Thiếu khóa → `AppKeyUnavailableError`.

    `override` (từ `Settings.app_key`) thắng keystore, và đó là thứ tự đúng: nó
    là đường của test, CI và bản cài container — nơi keystore hoặc không có,
    hoặc có nhưng thuộc về người dùng khác với người chạy dịch vụ.
    """
    raw = override if override else _read_from_keystore(service)
    if not raw:
        raise AppKeyUnavailableError(
            "Chưa có khóa mã hóa ứng dụng. Chạy `python -m ket.admin generate-app-key` "
            "trên máy chạy app server, hoặc đặt biến môi trường KET_APP_KEY.",
            service=service,
        )
    # `UnicodeEncodeError` cũng là "sai định dạng": khóa Fernet là base64 nên
    # thuần ASCII, và một giá trị có dấu tiếng Việt trong keystore chỉ có thể là
    # người dán nhầm thứ khác vào đó. Không bắt ở đây thì lỗi nổi lên dạng
    # `UnicodeEncodeError` thô giữa luồng đăng nhập, không nêu được cách sửa.
    try:
        key = raw.encode("ascii")
        Fernet(key)
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise AppKeyUnavailableError(
            "Khóa mã hóa ứng dụng sai định dạng — phải là khóa Fernet base64 32 byte",
            service=service,
        ) from exc
    return key


class SecretBox:
    """Mã hóa/giải mã bí mật lưu trong DB.

    `Fernet` = AES-128-CBC + HMAC-SHA256, có nhãn thời gian và xác thực toàn
    vẹn. Chọn nó thay vì tự ghép AES-GCM vì thứ hay hỏng trong mã tự ghép là
    quản lý nonce, và ở đây không có gì đáng phải tự ghép.
    """

    __slots__ = ("_fernet",)

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        """Ciphertext dạng bytes — cột `bytea`, không phải `text`.

        Kiểu cột là lớp phòng thủ thứ hai: một lệnh `UPDATE users SET
        totp_secret_enc = 'JBSWY3…'` viết nhầm giá trị dạng rõ sẽ bị PostgreSQL
        từ chối thay vì lặng lẽ nhận.
        """
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Giải mã; ciphertext hỏng hoặc sai khóa → `AppKeyUnavailableError`.

        Sai khóa là ca thật: khôi phục DB sang máy khác mà quên mang khóa. Báo
        đúng nguyên nhân ("khóa không mở được bí mật này") thay vì để
        `InvalidToken` thô nổi lên giữa luồng đăng nhập.
        """
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise AppKeyUnavailableError(
                "Khóa mã hóa hiện tại không mở được bí mật đã lưu — nhiều khả năng DB được "
                "khôi phục sang máy khác mà chưa mang theo khóa trong OS keystore."
            ) from exc
