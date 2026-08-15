"""Băm và kiểm mật khẩu (FR-NFR-010).

Argon2id, không phải bcrypt/PBKDF2: nó là thuật toán được khuyến nghị hiện nay
và là mặc định duy nhất trong `argon2-cffi` không phải chọn tay.

Tham số ghi tường minh chứ không dùng mặc định của thư viện, vì mặc định **đổi
giữa các bản**. Đổi không báo trước nghĩa là: hoặc mọi bản hash cũ bỗng cần
rehash cùng lúc, hoặc chi phí băm âm thầm giảm. Ghim ở đây thì mỗi lần đổi là
một dòng diff có người đọc.
"""

from __future__ import annotations

from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ket.kernel.errors import WeakPasswordError

_HASHER: Final = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
"""~64 MiB và ~50–100 ms mỗi lần băm trên máy văn phòng.

Cân theo bối cảnh: đăng nhập là thao tác thưa (một lần mỗi buổi làm việc, 5–50
người), nên chi phí này không nằm trên đường nóng nào. Máy chạy app server đồng
thời là máy chạy PostgreSQL ở chế độ một-máy, nên không đẩy `memory_cost` cao
hơn — băm mật khẩu không được cạnh tranh bộ nhớ với `shared_buffers`."""

MIN_LENGTH: Final[int] = 10
"""Ngắn hơn 10 ký tự thì mọi luật về ký tự đều vô nghĩa trước một cuộc dò offline.

10 chứ không phải 16: người dùng là kế toán viên gõ mật khẩu này mỗi sáng, và
một ngưỡng quá cao đẩy họ sang giấy nhớ dán màn hình — tức là hạ bảo mật thật
xuống trong khi nâng con số trên tài liệu lên."""

MIN_CHARACTER_CLASSES: Final[int] = 3
"""Trên bốn nhóm: chữ thường, chữ hoa, chữ số, ký tự khác."""

_DUMMY_HASH: Final[str] = (
    "$argon2id$v=19$m=65536,t=3,p=1$"
    "/4GyPbzGzWW27mYJOYa7Rg$CWubj0ely4I3haYERTHu25t+vobSz66MXwtntOC3pVk"
)
"""Hash cố định để `verify_dummy` tốn đúng công của một lần kiểm thật.

Hằng số chứ không phải băm lúc import: băm lúc import thêm ~100 ms vào thời gian
khởi động **và** vào mọi lần chạy test, cho một giá trị không ai cần đọc.

Phải là hash **thật, đúng tham số hiện hành**: `argon2-cffi` phân tích chuỗi
trước khi tính, nên một chuỗi bịa sẽ ném `InvalidHashError` sau ~0 ms và đường
"tài khoản không tồn tại" lại nhanh hơn hẳn đường thật — đúng thứ hàm này sinh
ra để xóa.

Hai test canh hai nửa, và **không** test nào đo thời gian (đo thời gian trong CI
là nguồn test chập chờn): `test_dummy_hash_matches_current_parameters` khẳng định
chuỗi này phân tích được và đúng tham số; `test_unknown_username_still_pays_for_a
_hash` khẳng định đường đăng nhập **có gọi** hàm này.

Mật khẩu gốc không có ý nghĩa và không dùng ở đâu khác."""


def hash_password(password: str) -> str:
    """Băm mật khẩu. Không kiểm chính sách ở đây — xem `validate_policy`."""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Mật khẩu có khớp hash không. Hash hỏng/không đọc được → `False`.

    Hash hỏng trả `False` thay vì ném: một dòng `users` bị sửa tay thành rác
    không được biến thành lỗi 500 trên màn hình đăng nhập của cả doanh nghiệp.

    `UnicodeEncodeError` nằm trong danh sách bắt vì `argon2-cffi` mã hóa **chuỗi
    hash** bằng ASCII (đúng theo đặc tả định dạng) và ném thẳng lỗi mã hóa khi
    gặp ký tự ngoài ASCII — một nhánh mà `InvalidHashError` không phủ.
    """
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, UnicodeEncodeError):
        return False


def verify_dummy(password: str) -> None:
    """Tiêu đúng lượng công của một lần kiểm, cho tài khoản **không tồn tại**.

    Thiếu bước này thì thời gian phản hồi tự nó là một API: tên đăng nhập có
    thật mất ~80 ms (có băm), tên bịa mất ~2 ms (thoát sớm). Ai cũng liệt kê
    được danh sách nhân sự của doanh nghiệp bằng một vòng lặp.
    """
    try:
        _HASHER.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, UnicodeEncodeError):
        return


def needs_rehash(password_hash: str) -> bool:
    """Hash được tạo bằng tham số cũ hơn tham số hiện tại.

    Gọi sau khi xác thực **thành công** — đó là lần duy nhất có mật khẩu dạng rõ
    trong tay để băm lại. Nhờ vậy nâng tham số về sau không cần bắt ai đổi mật
    khẩu.
    """
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except (InvalidHashError, UnicodeEncodeError):
        return True


def _character_classes(password: str) -> int:
    return sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )


def validate_policy(password: str, *, username: str) -> None:
    """Chính sách tối thiểu (FR-NFR-010). Vi phạm → `WeakPasswordError`.

    `details.rule` cho client biết **luật nào** hỏng để dựng câu tiếng Việt cụ
    thể; server không tự dựng câu hiển thị (plan.md §Quy ước REST API).
    """
    if len(password) < MIN_LENGTH:
        raise WeakPasswordError(
            f"Mật khẩu phải dài ít nhất {MIN_LENGTH} ký tự",
            rule="min_length",
            required=MIN_LENGTH,
        )
    if _character_classes(password) < MIN_CHARACTER_CLASSES:
        raise WeakPasswordError(
            f"Mật khẩu phải có ít nhất {MIN_CHARACTER_CLASSES} trong 4 nhóm ký tự: "
            "chữ thường, chữ hoa, chữ số, ký tự đặc biệt",
            rule="character_classes",
            required=MIN_CHARACTER_CLASSES,
        )
    if username and username.casefold() in password.casefold():
        raise WeakPasswordError(
            "Mật khẩu không được chứa tên đăng nhập",
            rule="contains_username",
        )
