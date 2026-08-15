"""Mã một lần theo thời gian: cửa sổ chấp nhận và chống dùng lại (FR-NFR-016).

Chống dùng lại là phần `pyotp` **không** làm hộ, nên nó là phần đáng kiểm nhất:
một mã sống 30 giây trong khi cửa sổ chấp nhận rộng ~90 giây, nên không chặn thì
ai đọc trộm được mã — qua vai, qua ảnh chụp màn hình chia sẻ — vẫn dùng lại được
trong cùng cửa sổ đó.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from ket.kernel.errors import InvalidTotpCodeError, TotpCodeReusedError
from ket.kernel.security import totp

NOW = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)


def _code_at(secret: str, moment: datetime) -> str:
    return pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS).at(moment)


def test_accepts_the_current_code_and_returns_its_time_step() -> None:
    secret = totp.generate_secret()
    counter = totp.verify_code(secret, _code_at(secret, NOW), last_counter=None, now=NOW)
    assert counter == int(NOW.timestamp()) // totp.PERIOD_SECONDS


def test_accepts_a_code_from_the_neighbouring_window() -> None:
    """Đồng hồ máy trạm trong LAN lệch vài chục giây là chuyện thường."""
    secret = totp.generate_secret()
    earlier = NOW - timedelta(seconds=totp.PERIOD_SECONDS)
    assert totp.verify_code(secret, _code_at(secret, earlier), last_counter=None, now=NOW)


def test_rejects_a_code_beyond_the_window() -> None:
    secret = totp.generate_secret()
    stale = NOW - timedelta(seconds=totp.PERIOD_SECONDS * 5)
    with pytest.raises(InvalidTotpCodeError):
        totp.verify_code(secret, _code_at(secret, stale), last_counter=None, now=NOW)


def test_rejects_a_wrong_code() -> None:
    secret = totp.generate_secret()
    with pytest.raises(InvalidTotpCodeError):
        totp.verify_code(secret, "000000", last_counter=int(NOW.timestamp()), now=NOW)


def test_rejects_a_code_that_was_already_used() -> None:
    """Bất biến trung tâm: cùng một mã không dùng được hai lần."""
    secret = totp.generate_secret()
    code = _code_at(secret, NOW)
    counter = totp.verify_code(secret, code, last_counter=None, now=NOW)

    with pytest.raises(TotpCodeReusedError):
        totp.verify_code(secret, code, last_counter=counter, now=NOW)


def test_rejects_an_older_code_after_a_newer_one_was_used() -> None:
    """Mã của bước **trước** vẫn nằm trong cửa sổ, nhưng đã bị vượt qua."""
    secret = totp.generate_secret()
    previous_step = NOW - timedelta(seconds=totp.PERIOD_SECONDS)
    current = totp.verify_code(secret, _code_at(secret, NOW), last_counter=None, now=NOW)

    with pytest.raises(TotpCodeReusedError):
        totp.verify_code(secret, _code_at(secret, previous_step), last_counter=current, now=NOW)


def test_accepts_the_next_code_after_one_was_used() -> None:
    """Chống dùng lại không được biến thành khóa tài khoản: mã kế tiếp phải đi qua."""
    secret = totp.generate_secret()
    used = totp.verify_code(secret, _code_at(secret, NOW), last_counter=None, now=NOW)
    later = NOW + timedelta(seconds=totp.PERIOD_SECONDS)
    assert totp.verify_code(secret, _code_at(secret, later), last_counter=used, now=later) > used


def test_ignores_spaces_users_type_between_digit_groups() -> None:
    secret = totp.generate_secret()
    code = _code_at(secret, NOW)
    spaced = f"{code[:3]} {code[3:]}"
    assert totp.verify_code(secret, spaced, last_counter=None, now=NOW)


def test_provisioning_uri_carries_issuer_and_account() -> None:
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, username="ketoan")
    assert uri.startswith("otpauth://totp/")
    assert secret in uri
    assert "ketoan" in uri
