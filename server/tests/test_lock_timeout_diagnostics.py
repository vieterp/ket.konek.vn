"""Cổng canh cho bộ chẩn đoán chờ khóa (`tests/lock_diagnostics.py`).

Món này chỉ có giá trị vào đúng ngày nó nổ — mà ngày ấy hiếm và không tái hiện
được theo ý muốn (lượt CI 33658137670: treo 28 phút ở
`pg_advisory_xact_lock`, không lặp lại lần nào sau đó). Một cơ chế chẩn đoán
chưa từng chạy thật là cơ chế **không ai biết là đã hỏng** — nên ở đây dựng
đúng cảnh treo ấy trong một giây và kiểm cả ba mắt xích: ngưỡng có áp không,
lỗi có nhận diện được không, bản đổ có nêu đích danh phía đang giữ khóa không.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from conftest import TEST_DATABASE, _admin_dsn
from lock_diagnostics import (
    LOCK_TIMEOUT_SQLSTATE,
    describe_blocking_sessions,
    is_lock_timeout,
)

pytestmark = pytest.mark.db

PROBE_KEY = "lock-timeout-diagnostics-probe"


def test_every_connection_to_the_test_database_inherits_the_lock_timeout(
    owner_engine: Engine,
) -> None:
    """Ngưỡng phải tới từ chính DATABASE, không từ engine nào đặt riêng.

    Đọc `pg_settings.setting` (đơn vị mili giây) chứ không `SHOW lock_timeout`:
    `SHOW` trả chuỗi đã được PostgreSQL làm đẹp (`'60s'` hiện ra là `'1min'`),
    nên khẳng định trên chuỗi sẽ đỏ khi ai đó chỉnh giá trị mà không đổi ý
    nghĩa. Con số mili giây thì không mơ hồ.

    Engine này **không** làm gì đặc biệt — và đó chính là điều đang được kiểm:
    một connection bất kỳ, kể cả do mã production tự dựng, phải thừa hưởng
    ngưỡng. Nếu ai đó chuyển `apply_lock_timeout` về đặt trong `connect_args`
    của từng engine trong conftest, bài này đỏ.
    """
    with owner_engine.connect() as connection:
        setting = connection.execute(
            text("SELECT setting FROM pg_settings WHERE name = 'lock_timeout'")
        ).scalar_one()
    assert setting != "0", (
        "database test không có `lock_timeout` — một lần chờ khóa sẽ treo tới trần job CI "
        "thay vì hỏng nhanh kèm SQLSTATE. Xem `lock_diagnostics.apply_lock_timeout`."
    )


def test_a_blocked_advisory_lock_fails_fast_and_names_the_blocking_session(
    owner_engine: Engine,
) -> None:
    """Dựng đúng cảnh đã treo trên CI, rồi kiểm cả chuỗi chẩn đoán.

    Phiên chờ tự hạ ngưỡng xuống 1 giây thay vì dùng 60s của database: bài này
    kiểm **cơ chế**, và bắt bộ test đứng một phút để chứng minh một điều đã biết
    là cái giá không ai nên trả. Ngưỡng mặc định do bài trên canh.
    """
    with owner_engine.begin() as blocker:
        blocker_pid = blocker.execute(text("SELECT pg_backend_pid()")).scalar_one()
        blocker.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": PROBE_KEY})

        with owner_engine.connect() as waiter:
            waiter.exec_driver_sql("SET lock_timeout = '1s'")
            with pytest.raises(OperationalError) as raised:
                waiter.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": PROBE_KEY}
                )

        # Đúng SQLSTATE, không phải "một lỗi nào đó": hook trong conftest lọc
        # theo mã này, nhận nhầm thì bản đổ sẽ không bao giờ xuất hiện.
        assert getattr(raised.value.orig, "sqlstate", None) == LOCK_TIMEOUT_SQLSTATE
        assert is_lock_timeout(raised.value)

        # Bản đổ phải chạy TRONG lúc phía kia còn giữ khóa — đó là điều kiện
        # duy nhất nó có gì để nói, và cũng đúng tình huống thật: phía giữ khóa
        # ở lượt CI treo không bao giờ nhả.
        dump = describe_blocking_sessions(_admin_dsn(), TEST_DATABASE)

    assert str(blocker_pid) in dump, (
        f"bản đổ không nêu phiên đang giữ khóa (pid {blocker_pid}):\n{dump}"
    )
    assert "advisory" in dump
