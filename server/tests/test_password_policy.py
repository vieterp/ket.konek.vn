"""Chính sách mật khẩu và băm Argon2id (FR-NFR-010).

Test đáng giá nhất trong tệp này là `test_dummy_hash_matches_current_parameters`
— nó canh một bất biến **không quan sát được** từ hành vi: hash giả dùng cho tài
khoản không tồn tại phải là hash **thật, đúng tham số hiện hành**. Một chuỗi bịa
vẫn khiến mọi test đăng nhập xanh, nhưng `argon2-cffi` sẽ ném ngay khi phân tích
chuỗi và nhánh "không có tài khoản" trở nên nhanh hơn nhánh thật hàng chục lần —
tức là khôi phục đúng kênh rò mà `verify_dummy` sinh ra để bịt.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from ket.kernel.errors import WeakPasswordError
from ket.kernel.security import passwords

GOOD_PASSWORD = "Ph1eu#Thu2026"
USERNAME = "ketoan"


def test_accepts_a_password_that_meets_every_rule() -> None:
    passwords.validate_policy(GOOD_PASSWORD, username=USERNAME)


@pytest.mark.parametrize(
    ("password", "rule"),
    [
        ("Abc#12de", "min_length"),
        ("matkhaudainhung", "character_classes"),
        ("Ketoan#2026vn", "contains_username"),
    ],
)
def test_rejects_and_names_the_violated_rule(password: str, rule: str) -> None:
    """`details.rule` là thứ client dựng câu tiếng Việt cụ thể từ đó."""
    with pytest.raises(WeakPasswordError) as raised:
        passwords.validate_policy(password, username=USERNAME)
    assert raised.value.details["rule"] == rule


def test_username_check_ignores_letter_case() -> None:
    """`KeToAn` trong mật khẩu vẫn là tên đăng nhập — người dò không phân biệt hoa thường."""
    with pytest.raises(WeakPasswordError) as raised:
        passwords.validate_policy("xxKeToAn#2026", username="ketoan")
    assert raised.value.details["rule"] == "contains_username"


def test_hash_verifies_and_rejects_the_wrong_password() -> None:
    hashed = passwords.hash_password(GOOD_PASSWORD)
    assert hashed != GOOD_PASSWORD
    assert passwords.verify_password(hashed, GOOD_PASSWORD) is True
    assert passwords.verify_password(hashed, GOOD_PASSWORD + "x") is False


def test_unreadable_hash_is_a_failed_login_not_a_crash() -> None:
    """Một dòng `users` bị sửa tay thành rác không được thành lỗi 500 cho cả DN."""
    assert passwords.verify_password("không-phải-hash", GOOD_PASSWORD) is False
    assert passwords.needs_rehash("không-phải-hash") is True


def test_current_hash_does_not_need_rehash() -> None:
    assert passwords.needs_rehash(passwords.hash_password(GOOD_PASSWORD)) is False


def test_hash_from_weaker_parameters_is_flagged_for_rehash() -> None:
    """Nâng tham số băm phải tự lan ra mà không bắt ai đổi mật khẩu."""
    weaker = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1).hash(GOOD_PASSWORD)
    assert passwords.needs_rehash(weaker) is True


def test_dummy_hash_matches_current_parameters() -> None:
    """Hash giả phải **thật** và đúng tham số — nếu không, nhánh "không có tài
    khoản" thoát sớm và thời gian phản hồi lại liệt kê được người dùng.

    `needs_rehash` trả `False` chỉ khi chuỗi phân tích được **và** mọi tham số
    khớp bộ hiện hành. Đó là cách kiểm rẻ nhất mà không phải đo thời gian (đo
    thời gian trong CI là nguồn test chập chờn).
    """
    assert passwords.needs_rehash(passwords._DUMMY_HASH) is False


def test_verify_dummy_never_raises() -> None:
    """Nó nằm trên đường đăng nhập thất bại — ném ở đó là biến 401 thành 500."""
    passwords.verify_dummy("bất kỳ thứ gì")
