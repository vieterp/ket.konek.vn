"""Hai cơ chế của luồng đăng nhập chỉ đúng khi có khóa hàng (`FOR UPDATE`).

Bộ test này ra đời từ một phát hiện của vòng review: cả hai cơ chế dưới đây là
**đọc–sửa–ghi** trên cùng một dòng `users`, và bộ test tuần tự không thấy được
điều đó. Đo trên cụm thật trước khi sửa:

    10 lần sai ĐỒNG THỜI  → failed_login_count = 1,  locked_until = None
    10 lần sai TUẦN TỰ    → failed_login_count = 0,  locked_until = <đã khóa>

Nói cách khác: hàng phòng thủ duy nhất chống dò mật khẩu biến mất với bất kỳ ai
mở nhiều kết nối, và một mã TOTP dùng song song cấp được nhiều phiên.

Cả hai test đều chạy **thật sự song song** bằng thread; dùng `NullPool` nên mỗi
thread có connection riêng, tức là có transaction riêng — đúng hình dạng của
nhiều request đồng thời trên bản cài LAN.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.models import User
from ket.kernel.errors import DomainError
from ket.kernel.persistence.session import control_session
from ket.kernel.security import account_service, auth_service, totp
from ket.kernel.security.keystore import SecretBox

pytestmark = [pytest.mark.db, pytest.mark.real_password_hashing]
"""Giữ THAM SỐ ARGON2 THẬT cho tệp này.

`conftest.cheap_password_hashing` hạ chi phí băm cho cả bộ test. Ở đây điều đó
làm hỏng chính thứ đang kiểm: cửa sổ đua của `auth_service` là quãng
`SELECT … FOR UPDATE` → verify mật khẩu → ghi số lần sai, và **độ rộng của nó
gần bằng thời gian băm**. Hạ băm từ ~80ms xuống ~0,1ms thu cửa sổ ấy lại ngót
ba bậc, nên một hồi quy tương lai (ví dụ ai đó bỏ `with_for_update()`) vẫn có
thể xanh vì hai thread không kịp chen vào nhau. Bài vẫn *chạy*, chỉ là gần như
không còn bắt được gì — nên nó phải chạy trên tham số thật (review pre-landing).
"""

PASSWORD = "Ph1eu#Thu2026"
WRONG_PASSWORD = "Sai#MatKhau2026"
SESSION_TTL = auth_service.LOCKOUT_DURATION
UserFactory = Callable[..., User]


def _attempt(job: Callable[[], object]) -> object | None:
    """Chạy một lần thử, nuốt lỗi nghiệp vụ — test quan tâm **trạng thái để lại**."""
    try:
        return job()
    except DomainError:
        return None


def test_simultaneous_wrong_passwords_still_lock_the_account(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Đủ số lần sai thì khóa, dù chúng đến cùng lúc hay nối đuôi nhau."""
    user = user_factory("dongthoi_sai")

    def try_login() -> object:
        return auth_service.authenticate(
            session_factory,
            username=user.username,
            password=WRONG_PASSWORD,
            secret_box_provider=_no_key_needed,
            session_ttl=SESSION_TTL,
        )

    with ThreadPoolExecutor(max_workers=auth_service.LOCKOUT_THRESHOLD) as pool:
        list(pool.map(lambda _: _attempt(try_login), range(auth_service.LOCKOUT_THRESHOLD)))

    with control_session(session_factory) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        assert reloaded.locked_until is not None, (
            "10 lần sai đồng thời phải khóa tài khoản y như 10 lần sai tuần tự — "
            "nếu không, mở nhiều kết nối là vượt được hàng phòng thủ duy nhất"
        )


def test_the_same_totp_code_cannot_buy_two_sessions(
    session_factory: sessionmaker[Session], user_factory: UserFactory, secret_box: SecretBox
) -> None:
    """Chống phát lại mã phải đứng vững dưới đồng thời, không chỉ khi thử lại tuần tự."""
    user = user_factory("dongthoi_totp", totp_required=True)
    secret = _enrol(session_factory, user.username, secret_box)
    code = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS).at(
        datetime.now(UTC)
    )

    def try_login() -> object:
        return auth_service.authenticate(
            session_factory,
            username=user.username,
            password=PASSWORD,
            totp_code=code,
            secret_box_provider=lambda: secret_box,
            session_ttl=SESSION_TTL,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _attempt(try_login), range(4)))

    issued = [result for result in results if result is not None]
    assert len(issued) == 1, (
        "một mã TOTP chỉ được đổi lấy đúng một phiên; nhiều hơn nghĩa là ai đọc trộm "
        "được mã vẫn dùng lại được trong cùng cửa sổ 30 giây"
    )


def _enrol(factory: sessionmaker[Session], username: str, secret_box: SecretBox) -> str:
    """Đăng ký và xác nhận thiết bị sinh mã, trả về bí mật dạng rõ cho test.

    Xác nhận bằng mã của **bước thời gian trước đó**, không phải bước hiện tại:
    xác nhận cũng ghi `totp_last_counter`, nên nếu dùng mã của bước hiện tại thì
    chính bước ấy đã bị tiêu, và lần đăng nhập ngay sau đó bị từ chối vì "dùng
    lại mã" — test sẽ đỏ vì một lý do đúng nhưng không phải lý do nó đang kiểm.
    """
    with control_session(factory) as session:
        uri = account_service.begin_totp_enrollment(
            session, user=account_service.find_user(session, username), secret_box=secret_box
        )
    secret = uri.split("secret=")[1].split("&")[0]

    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    earlier = datetime.now(UTC) - timedelta(seconds=totp.PERIOD_SECONDS)
    with control_session(factory) as session:
        account_service.confirm_totp_enrollment(
            session,
            user=account_service.find_user(session, username),
            code=generator.at(earlier),
            secret_box=secret_box,
            now=earlier,
        )
    return secret


def _no_key_needed() -> SecretBox:
    raise AssertionError("tài khoản không bật 2FA thì không được chạm tới khóa mã hóa")
