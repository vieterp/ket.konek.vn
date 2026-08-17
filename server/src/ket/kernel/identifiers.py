"""Định danh ổn định cho danh mục — UUIDv7 (RT-19, RFC 9562 §5.7).

Vì sao danh mục cần **hai** khóa. Khóa `int` là thứ mọi bảng phát sinh tham
chiếu: một sổ cái một năm có hàng triệu dòng, và mỗi dòng mang thêm 12 byte cho
mỗi chiều phân tích là chi phí trả suốt vòng đời sản phẩm, trên đúng những cột
được đánh chỉ mục và join nhiều nhất. Nhưng khóa `int` do sequence cấp nên nó
chỉ có nghĩa **bên trong một bản cài**: chi nhánh Hà Nội và chi nhánh Đà Nẵng
đều có "khách hàng số 47", và hai người đó không phải một người.

`uid` giải đúng chuyện đó và chỉ chuyện đó: khi bản cài chi nhánh tổng hợp dữ
liệu lên trụ sở (cam kết một chiều của v1 — plan.md §Phạm vi cắt, RT-19), `uid`
là thứ nói "hai dòng này là cùng một danh mục". Không có nó thì việc nối phải
khớp theo `code` — mà `code` là dữ liệu người dùng sửa được, nên mỗi lần ai đó
sửa mã khách hàng là một lần lịch sử đối chiếu đứt.

Vì sao **v7** chứ không v4: `uid` có chỉ mục duy nhất, và v4 ngẫu nhiên đều làm
mỗi lần chèn rơi vào một trang B-tree khác nhau — chỉ mục phình và cache miss
tăng theo số bản ghi. v7 mang 48 bit thời gian ở đầu nên bản ghi mới luôn chèn
vào cuối cây, đúng đặc tính của khóa tăng dần mà không mất tính toàn cục.

Vì sao **tự viết** chứ không thêm thư viện: `uuid.uuid7()` chỉ có từ Python 3.14
(bản cài đích là 3.12), còn `uuid-utils` là extension Rust — thêm một native
dependency vào lúc spike S4 (đóng gói server thành installer một-bấm) đang là
rủi ro Cao của phase 2 thì phải có lý do mạnh hơn 30 dòng mã có test.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Final
from uuid import UUID

UUID_VERSION_7: Final[int] = 7
"""Số hiệu phiên bản ghi vào 4 bit cao của octet thứ 7 (RFC 9562 §4.2)."""

_TIMESTAMP_BITS: Final[int] = 48
_COUNTER_BITS: Final[int] = 12
"""`rand_a` dùng làm bộ đếm đơn điệu (RFC 9562 §6.2, "Method 1").

12 bit = 4096 giá trị cho mỗi mili-giây. Nhập liệu kế toán không tới gần con số
đó, nhưng nhập Excel 10.000 dòng (FR-NFR-043) thì có — nên tràn bộ đếm là
trường hợp phải xử lý chứ không phải giả định."""

_COUNTER_MAX: Final[int] = (1 << _COUNTER_BITS) - 1
_RANDOM_BITS: Final[int] = 62

_lock = threading.Lock()
_last_timestamp_ms: int = 0
_counter: int = 0


def _now_ms() -> int:
    """Đồng hồ tường, tính bằng mili-giây.

    Tách thành hàm riêng để test đóng băng được **đúng chỗ này** thay vì vá
    `time.time_ns` toàn tiến trình (thứ mọi thư viện khác cũng đang gọi). Đóng
    băng đồng hồ là cách duy nhất dựng được cuộc đua thật trên bộ đếm — xem
    `tests/test_uuid7_identifiers.py`.
    """
    return time.time_ns() // 1_000_000


def _next_timestamp_and_counter() -> tuple[int, int]:
    """Cấp cặp (mốc thời gian, bộ đếm) đơn điệu không giảm.

    Khóa quanh **cả** phép đọc đồng hồ lẫn phép cập nhật trạng thái. Cái nó bảo
    vệ là **tính đơn điệu**, không phải tính duy nhất: 62 bit ngẫu nhiên phía
    dưới đã khiến hai `uid` trùng nhau là chuyện gần như không xảy ra dù có khóa
    hay không. Mất khóa thì `_counter += 1` mất lượt, hai `uid` sinh sau nhau
    nhận cùng một tiền tố (mốc thời gian + bộ đếm), và thứ tự chèn thôi không
    còn khớp thứ tự khóa — tức là mất đúng tính chất khiến v7 được chọn thay v4.
    Hỏng kiểu đó không có triệu chứng nào ngoài chỉ mục phình dần.

    Đồng hồ chạy lùi (NTP hiệu chỉnh, người dùng đổi giờ hệ thống) được xử lý
    bằng cách **giữ nguyên** mốc cũ và nhích bộ đếm: `uid` không còn khớp tuyệt
    đối với thời gian thực trong vài mili-giây, đổi lấy việc chỉ mục không bao
    giờ bị chèn ngược và tính duy nhất không bao giờ thủng.
    """
    global _last_timestamp_ms, _counter

    with _lock:
        now_ms = _now_ms()
        if now_ms > _last_timestamp_ms:
            _last_timestamp_ms = now_ms
            _counter = 0
        elif _counter < _COUNTER_MAX:
            _counter += 1
        else:
            # Hết chỗ trong mili-giây này: mượn của mili-giây kế tiếp. Vẫn đơn
            # điệu, vẫn duy nhất; chỉ là mốc thời gian chạy trước đồng hồ thật
            # một chút cho tới khi đồng hồ đuổi kịp.
            _last_timestamp_ms += 1
            _counter = 0
        return _last_timestamp_ms, _counter


def uuid7() -> UUID:
    """Sinh một UUIDv7: 48 bit mili-giây, 12 bit bộ đếm, 62 bit ngẫu nhiên.

    Phần ngẫu nhiên lấy từ `secrets` chứ không `random`: `uid` xuất hiện trong
    URL và trong tệp xuất ra ngoài, nên đoán được `uid` kế tiếp là đoán được có
    bao nhiêu danh mục vừa được tạo — một rò rỉ nhỏ nhưng miễn phí để bịt.
    """
    timestamp_ms, counter = _next_timestamp_and_counter()
    random_bits = secrets.randbits(_RANDOM_BITS)

    value = timestamp_ms << (128 - _TIMESTAMP_BITS)
    value |= UUID_VERSION_7 << 76
    value |= counter << 64
    value |= 0b10 << 62  # variant RFC 9562
    value |= random_bits
    return UUID(int=value)


def timestamp_ms_of(value: UUID) -> int:
    """Đọc lại mốc thời gian đã nhúng — dùng cho test và cho công cụ chẩn đoán."""
    if value.version != UUID_VERSION_7:
        raise ValueError(f"Chỉ UUIDv7 mang mốc thời gian, nhận phiên bản {value.version}")
    return value.int >> (128 - _TIMESTAMP_BITS)
