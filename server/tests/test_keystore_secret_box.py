"""Khóa app và mã hóa bí mật lưu trong DB (ADR-019, RT-05).

Bất biến chính: **thiếu khóa thì hỏng, không phải lưu dạng rõ.** Mọi test dưới
đây kiểm cùng một hướng hỏng — fail-closed — vì hướng hỏng ngược lại (âm thầm
lưu bí mật đọc được) không để lại triệu chứng nào cho tới khi ai đó mở bản sao
lưu ra đọc.
"""

from __future__ import annotations

import pytest

from ket.kernel.errors import AppKeyUnavailableError
from ket.kernel.security import keystore
from ket.kernel.security.keystore import SecretBox, generate_app_key, load_app_key

SERVICE = "ket-test"
SECRET = "JBSWY3DPEHPK3PXP"


def test_encrypt_then_decrypt_returns_the_original() -> None:
    box = SecretBox(generate_app_key().encode("ascii"))
    assert box.decrypt(box.encrypt(SECRET)) == SECRET


def test_ciphertext_is_bytes_and_hides_the_plaintext() -> None:
    """Cột là `bytea`; bản dump không được chứa bí mật ở dạng đọc được."""
    box = SecretBox(generate_app_key().encode("ascii"))
    ciphertext = box.encrypt(SECRET)
    assert isinstance(ciphertext, bytes)
    assert SECRET.encode("ascii") not in ciphertext


def test_encrypting_twice_gives_different_ciphertext() -> None:
    """Cùng bí mật, khác ciphertext — nếu không, so sánh hai dòng DB là suy ra
    được hai người dùng có chung bí mật."""
    box = SecretBox(generate_app_key().encode("ascii"))
    assert box.encrypt(SECRET) != box.encrypt(SECRET)


def test_another_key_cannot_open_the_secret() -> None:
    """Khôi phục DB sang máy khác mà quên mang khóa: phải báo đúng nguyên nhân."""
    ciphertext = SecretBox(generate_app_key().encode("ascii")).encrypt(SECRET)
    other = SecretBox(generate_app_key().encode("ascii"))
    with pytest.raises(AppKeyUnavailableError):
        other.decrypt(ciphertext)


def test_override_wins_over_the_operating_system_keystore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Đường của CI và của bản cài container: `KET_APP_KEY` phải thắng."""
    monkeypatch.setattr(keystore, "_read_from_keystore", lambda _service: "khóa-trong-keystore")
    key = generate_app_key()
    assert load_app_key(service=SERVICE, override=key) == key.encode("ascii")


def test_missing_key_fails_closed_with_a_fixable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keystore, "_read_from_keystore", lambda _service: None)
    with pytest.raises(AppKeyUnavailableError) as raised:
        load_app_key(service=SERVICE)
    assert "generate-app-key" in str(raised.value)


def test_malformed_key_is_rejected_at_load_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bắt lúc nạp chứ không lúc dùng: một khóa hỏng phát hiện giữa luồng đăng
    nhập thì đã muộn hơn một bước so với cần thiết."""
    monkeypatch.setattr(keystore, "_read_from_keystore", lambda _service: "không-phải-fernet")
    with pytest.raises(AppKeyUnavailableError):
        load_app_key(service=SERVICE)


def test_keystore_failure_is_reported_as_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Máy không có backend keystore (container Linux, dịch vụ chạy nền) phải
    cho ra "chưa có khóa", không phải một ngoại lệ của thư viện lọt lên tầng API."""
    import keyring
    from keyring.errors import KeyringError

    def explode(_service: str, _username: str) -> str | None:
        raise KeyringError("không có backend")

    monkeypatch.setattr(keyring, "get_password", explode)
    with pytest.raises(AppKeyUnavailableError):
        load_app_key(service=SERVICE)
