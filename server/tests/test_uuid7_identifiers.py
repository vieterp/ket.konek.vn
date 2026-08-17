"""UUIDv7 tự viết phải đúng RFC 9562 và phải **tăng dần** (RT-19).

Hai tính chất, hai lý do khác nhau:

* **Đúng định dạng** — `uid` rời khỏi hệ này (tệp xuất, gói tổng hợp lên trụ
  sở), nên phía nhận phải phân tích được nó bằng bất kỳ thư viện UUID nào.
* **Tăng dần** — đây là lý do chọn v7 thay vì v4. Mất tính chất này thì cột
  `uid` vẫn chạy đúng và chỉ mục vẫn phình theo số bản ghi, tức là hỏng theo
  kiểu không ai nhận ra cho tới khi bảng đủ lớn.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from ket.kernel import identifiers
from ket.kernel.identifiers import _RANDOM_BITS, UUID_VERSION_7, timestamp_ms_of, uuid7

_MUTUAL_EXCLUSION_TIMEOUT = 0.2
"""Thời gian chờ ở barrier trước khi kết luận "luồng kia không vào được".

Khi khóa còn nguyên thì đây chính là thời gian test tốn — nên giữ ngắn; khi khóa
mất thì barrier khớp ngay lập tức và không ai phải chờ."""


def test_it_is_a_well_formed_version_7_uuid() -> None:
    value = uuid7()
    assert value.version == UUID_VERSION_7
    assert value.variant == "specified in RFC 4122"


def test_the_embedded_timestamp_is_the_current_time() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= timestamp_ms_of(value) <= after


def test_values_generated_in_a_row_keep_increasing() -> None:
    """Trong cùng một mili-giây, bộ đếm `rand_a` giữ thứ tự.

    Sinh 5.000 giá trị liên tiếp — đủ để rơi vào cùng mili-giây nhiều lần và đủ
    để **tràn** bộ đếm 12 bit (4.096 giá trị/ms) nếu máy đủ nhanh, tức là kiểm
    luôn nhánh "mượn của mili-giây kế tiếp".
    """
    values = [uuid7() for _ in range(5_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_two_threads_are_never_inside_the_counter_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bộ đếm là vùng loại trừ lẫn nhau — kiểm thẳng điều đó (sửa sau review H3).

    Hai cách viết hiển nhiên hơn đều là **test rỗng**, cả hai đã đo:

    * "N luồng sinh uid, khẳng định không trùng" — 62 bit ngẫu nhiên phía dưới
      khiến trùng `uid` gần như không xảy ra **dù có khóa hay không**. Gỡ `_lock`
      đi vẫn xanh 3/3 lần.
    * "khẳng định tiền tố (mốc thời gian + bộ đếm) không lặp", kể cả khi đã đóng
      băng đồng hồ, siết `sys.setswitchinterval` và cho luồng ngủ trong vùng
      khóa — vẫn xanh 3/3 lần khi gỡ khóa, vì khoảng giữa lúc đọc `_counter` và
      lúc ghi lại nó quá hẹp để hai luồng rơi trúng.

    Cách ở đây không đợi may rủi mà **ép** hai luồng phải cùng có mặt: đồng hồ
    giả chặn tại một `Barrier` hai chỗ, và `_now_ms` được gọi **bên trong** vùng
    khóa. Còn khóa thì luồng thứ hai không vào nổi, barrier hết giờ và vỡ — đó
    là kết quả **đúng**. Mất khóa thì cả hai vào cùng lúc, barrier khớp, và cờ
    được bật.

    Bất biến thật sự đang canh: khóa bảo vệ **tính đơn điệu** của bộ đếm, không
    phải tính duy nhất của `uid`. Mất lượt đếm thì hai `uid` sinh sau nhau mang
    cùng tiền tố, thứ tự chèn thôi không khớp thứ tự khóa, và ta mất đúng tính
    chất khiến v7 được chọn thay v4 — một hỏng hóc không có triệu chứng nào
    ngoài chỉ mục phình dần.
    """
    threads = 2
    barrier = threading.Barrier(threads)
    entered_together = threading.Event()

    def clock_that_waits_for_a_second_thread() -> int:
        try:
            barrier.wait(timeout=_MUTUAL_EXCLUSION_TIMEOUT)
        except threading.BrokenBarrierError:
            pass  # đúng như mong đợi: luồng kia bị khóa chặn ngoài cửa
        else:
            entered_together.set()
        return 1_755_000_000_000

    monkeypatch.setattr(identifiers, "_now_ms", clock_that_waits_for_a_second_thread)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        values = list(pool.map(lambda _: uuid7(), range(threads)))

    assert len(values) == threads
    assert not entered_together.is_set(), "hai luồng cùng vào vùng cập nhật bộ đếm"

    # Hai giá trị cấp nối tiếp nhau phải khác **tiền tố** (mốc thời gian + bộ
    # đếm), không chỉ khác ở phần ngẫu nhiên — đó mới là thứ quyết định thứ tự.
    prefixes = [value.int >> _RANDOM_BITS for value in values]
    assert len(set(prefixes)) == threads


def test_reading_the_timestamp_of_another_version_is_refused() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        timestamp_ms_of(UUID("00000000-0000-4000-8000-000000000000"))
