"""Khóa công khai của publisher, ghim cứng trong mã nguồn (RT-07).

**Cố ý không đọc từ DB/biến môi trường.** Nếu khóa tin cậy nằm ở một hàng
trong DB thì bất kỳ ai ghi được hàng đó (một lỗi quyền, một bản khôi phục từ
nơi khác) đã có thể tự phong mình là publisher và gói `.zip` độc chạy được SQL
ký hiệu "builtin" (§Chống SQL injection của phase-05). Ghim trong mã nguồn biến
việc đổi khóa tin cậy thành một **bản phát hành phần mềm mới, có review** — thứ
duy nhất RT-07 chấp nhận.

Danh sách nhiều khóa (không phải một hằng số) để xoay khóa êm: phát hành một
bản có **cả khóa cũ lẫn khóa mới**, gói ký bằng khóa mới vẫn qua được, rồi gỡ
khóa cũ ở bản sau — không có cửa sổ nào gói hợp lệ bị từ chối.

Khóa v1 dưới đây sinh cho lát 5A (chưa có quy trình ký gói ngoài repo). Khóa
**riêng** không nằm trong mã nguồn hay tài liệu — bàn giao qua kênh an toàn
riêng (không phải Git) trước khi dùng ký gói thật; đây là việc vận hành, không
phải việc của module này.
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_PINNED_PEM_KEYS: Final[tuple[str, ...]] = (
    # v1 — lát 5A, 2026-08-19.
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAezIS/E1ZS5sppFVYcbUelfpatfSJQ96/WU9w41Y9sCs=\n"
    "-----END PUBLIC KEY-----\n",
)


def _load(pem: str) -> Ed25519PublicKey:
    key = load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("Khóa ghim trong publisher_keys.py phải là khóa Ed25519")
    return key


def pinned_public_keys() -> tuple[Ed25519PublicKey, ...]:
    """Bộ khóa công khai tin cậy của tiến trình — nguồn sự thật cho `signature_verifier.py`.

    Trả một **bản dựng mới** mỗi lần gọi thay vì một hằng số module-level đã
    nạp sẵn: đối tượng khóa từ `cryptography` không bất biến quan sát được từ
    bên ngoài theo cùng cách một `bytes`, và test cần dựng bộ khóa test riêng
    (`inject`) mà không đụng bộ khóa thật — hàm là điểm nối duy nhất.
    """
    return tuple(_load(pem) for pem in _PINNED_PEM_KEYS)
