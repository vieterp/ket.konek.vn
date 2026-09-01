"""Chính sách mật khẩu và băm Argon2id (FR-NFR-010).

Test đáng giá nhất trong tệp này là `test_dummy_hash_matches_current_parameters`
— nó canh một bất biến **không quan sát được** từ hành vi: hash giả dùng cho tài
khoản không tồn tại phải là hash **thật, đúng tham số hiện hành**. Một chuỗi bịa
vẫn khiến mọi test đăng nhập xanh, nhưng `argon2-cffi` sẽ ném ngay khi phân tích
chuỗi và nhánh "không có tài khoản" trở nên nhanh hơn nhánh thật hàng chục lần —
tức là khôi phục đúng kênh rò mà `verify_dummy` sinh ra để bịt.
"""

from __future__ import annotations

import threading

import pytest
from argon2 import PasswordHasher

from ket.kernel.errors import AuthThrottledError, WeakPasswordError
from ket.kernel.security import passwords

pytestmark = pytest.mark.real_password_hashing
"""Tệp này khẳng định trên THAM SỐ ARGON2 THẬT.

`conftest.cheap_password_hashing` hạ chi phí băm cho cả bộ test (16% CPU).
Chạy tệp này dưới bộ tham số rẻ sẽ khiến ba bài về `needs_rehash` xanh **rỗng**:
hash "yếu hơn" hoá ra mạnh hơn bộ đang chạy, và `_DUMMY_HASH` bị thay nên bài
canh hằng số production trở thành phép so một thứ với chính nó.

Bản đầu của bản vá dùng fixture phạm vi PHIÊN + khai trùng tên ở đây để "tắt".
Cách ấy không chạy — khai trùng không gỡ được bản vá đã áp từ module trước — và
review pre-landing bắt được bằng probe. Dấu `real_password_hashing` + fixture
phạm vi HÀM ở conftest mới thật sự miễn trừ.
"""


def test_the_real_argon2_parameters_are_live() -> None:
    """Cổng của chính tệp này: sai bộ tham số thì ĐỎ, không xanh rỗng.

    Không có bài này thì một lần nữa ai đó đổi cơ chế miễn trừ và mọi khẳng
    định dưới đây lặng lẽ mất hiệu lực — đúng chuyện vừa xảy ra.
    """
    assert passwords._HASHER.memory_cost == 64 * 1024
    assert passwords._HASHER.time_cost == 3
    assert passwords._DUMMY_HASH.startswith("$argon2id$v=19$m=65536,t=3,p=1$")


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


# --------------------------------------------------------------------------
# Hàng đợi băm — trần bộ nhớ của đường xác thực
# --------------------------------------------------------------------------


def test_hashing_is_capped_and_refuses_instead_of_queueing_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hết suất băm → 503 ngay, không xếp hàng vô hạn.

    Hai nửa của cùng một cơ chế. Không có trần: 40 luồng threadpool × 64 MiB
    ≈ 2,5 GB trên đúng cái máy chạy PostgreSQL ở chế độ một-máy, và không cần
    tài khoản hợp lệ mới ép được (nhánh "không có tài khoản" cũng băm). Có trần
    mà chờ vô hạn: đổi cạn bộ nhớ lấy cạn luồng, và mọi endpoint đồng bộ khác —
    tức gần như toàn bộ API — đứng theo.
    """
    monkeypatch.setattr(passwords, "_HASH_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(passwords, "HASH_WAIT_SECONDS", 0)
    passwords._HASH_SLOTS.acquire()

    with pytest.raises(AuthThrottledError) as error:
        passwords.hash_password(GOOD_PASSWORD)
    assert error.value.http_status == 503

    # Nhánh tài khoản-không-tồn-tại đi qua **cùng** hàng đợi: nếu không, trần
    # chỉ ràng buộc được tài khoản có thật.
    with pytest.raises(AuthThrottledError):
        passwords.verify_dummy(GOOD_PASSWORD)


def test_a_failed_verification_gives_its_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rò suất là DoS tự gây: vài lần sai mật khẩu và server ngừng xác thực được."""
    monkeypatch.setattr(passwords, "_HASH_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(passwords, "HASH_WAIT_SECONDS", 0)

    assert passwords.verify_password("không-phải-hash", GOOD_PASSWORD) is False
    assert passwords.verify_password(passwords.hash_password(GOOD_PASSWORD), "sai") is False
    # Suất vẫn còn: lần băm kế tiếp không bị từ chối.
    assert passwords.hash_password(GOOD_PASSWORD)
