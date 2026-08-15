"""Đăng nhập, phiên và khóa tài khoản trên PostgreSQL thật (FR-NFR-010/016).

Chạy trên DB thật chứ không mock: bất biến đắt giá nhất của luồng này là
"**đường thất bại cũng phải commit**" — bộ đếm khóa và dòng `control_audit_log`
phải sống sót dù kết quả là một ngoại lệ. Bất biến đó chỉ quan sát được khi có
transaction thật; với một `Session` giả thì nó luôn xanh.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.control_log import ControlAuditAction, ControlAuditLog
from ket.kernel.datasets.models import User
from ket.kernel.errors import (
    AccountLockedError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    TotpCodeReusedError,
    TotpEnrollmentRequiredError,
    TotpRequiredError,
)
from ket.kernel.persistence.session import control_session
from ket.kernel.security import account_service, auth_service, totp
from ket.kernel.security.auth_models import AuthSession, token_digest
from ket.kernel.security.keystore import SecretBox

pytestmark = pytest.mark.db

TTL = timedelta(hours=12)

PASSWORD = "Ph1eu#Thu2026"
"""Bằng `conftest.TEST_PASSWORD` — mặc định của `user_factory`.

Chép giá trị thay vì import: `tests` không phải một gói Python nên
`from tests.conftest import …` chỉ chạy được tùy cách gọi pytest.
`test_default_password_matches_the_fixture` canh cho hai bên không trôi khỏi
nhau."""

WRONG_PASSWORD = "Sai#MatKhau2026"

UserFactory = Callable[..., User]


def test_default_password_matches_the_fixture(test_password: str) -> None:
    """Nếu `conftest` đổi mật khẩu mặc định, mọi test dưới đây sẽ hỏng vì lý do
    khó hiểu (mật khẩu "đúng" bỗng sai). Hỏng ở đây thì thông điệp nói thẳng."""
    assert PASSWORD == test_password


def _login(
    factory: sessionmaker[Session],
    username: str,
    password: str = PASSWORD,
    *,
    secret_box: SecretBox | None = None,
    totp_code: str | None = None,
    now: datetime | None = None,
) -> auth_service.IssuedSession:
    return auth_service.authenticate(
        factory,
        username=username,
        password=password,
        totp_code=totp_code,
        secret_box_provider=lambda: _require(secret_box),
        session_ttl=TTL,
        client_info="pytest",
        now=now,
    )


def _require(secret_box: SecretBox | None) -> SecretBox:
    """Provider của test: nổ nếu bị gọi trong ca lẽ ra không cần khóa.

    Đó là một khẳng định trá hình — tài khoản không bật 2FA mà vẫn nạp khóa app
    nghĩa là bản cài chưa cấu hình keystore sẽ không ai đăng nhập được.
    """
    assert secret_box is not None, "đường này lẽ ra không cần khóa mã hóa"
    return secret_box


def _audit_actions(factory: sessionmaker[Session], user_id: int) -> list[str]:
    with control_session(factory) as session:
        return list(
            session.scalars(
                select(ControlAuditLog.action)
                .where(ControlAuditLog.subject_user_id == user_id)
                .order_by(ControlAuditLog.id)
            )
        )


def _reload(factory: sessionmaker[Session], user_id: int) -> User:
    with control_session(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return user


def test_login_issues_a_session_and_leaves_a_trace(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("dangnhap")
    issued = _login(session_factory, user.username)

    assert issued.token
    assert issued.expires_at > datetime.now(UTC)
    assert _audit_actions(session_factory, user.id) == [
        ControlAuditAction.USER_CREATED.value,
        ControlAuditAction.LOGIN_SUCCEEDED.value,
    ]


def test_stored_session_holds_only_the_hash_of_the_token(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Đọc trộm `auth_sessions` không được đổi thành quyền đăng nhập."""
    user = user_factory("bammat")
    issued = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        row = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one()
        assert row.user_id == user.id
        assert issued.token.encode("ascii") not in row.token_hash


def test_wrong_password_is_recorded_and_counted(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Bất biến trung tâm: lỗi được ném **sau khi** transaction đã commit."""
    user = user_factory("saimatkhau")

    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, user.username, WRONG_PASSWORD)

    assert _reload(session_factory, user.id).failed_login_count == 1
    assert ControlAuditAction.LOGIN_FAILED.value in _audit_actions(session_factory, user.id)


def test_unknown_username_is_recorded_without_a_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    """Dò tài khoản phải để lại vết đọc được: `subject_label` giữ tên đã nhập."""
    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, "khong_ton_tai_bao_gio")

    with control_session(session_factory) as session:
        found = session.scalars(
            select(func.count())
            .select_from(ControlAuditLog)
            .where(
                ControlAuditLog.subject_label == "khong_ton_tai_bao_gio",
                ControlAuditLog.action == ControlAuditAction.LOGIN_FAILED.value,
                ControlAuditLog.subject_user_id.is_(None),
            )
        ).one()
    assert found == 1


def test_unknown_username_still_pays_for_a_hash(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chống liệt kê người dùng: nhánh "không có tài khoản" phải tốn đúng công.

    Kiểm bằng **điểm gọi** chứ không bằng đồng hồ: khẳng định thời gian trong CI
    là nguồn test chập chờn, còn "có gọi `verify_dummy` không" thì dứt khoát và
    đủ để giết đúng đột biến đáng lo — ai đó xóa lời gọi ấy đi vì "nó chẳng làm
    gì cả".
    """
    called: list[str] = []
    monkeypatch.setattr(
        "ket.kernel.security.passwords.verify_dummy", lambda password: called.append(password)
    )

    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, "khong_ton_tai_bao_gio_nua")

    assert called == [PASSWORD]


def test_disabled_account_also_pays_for_a_hash(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Ai đã nghỉ việc" không được trở thành thông tin đọc được từ bên ngoài."""
    user = user_factory("vohieu_nhanh", is_active=False)
    called: list[str] = []
    monkeypatch.setattr(
        "ket.kernel.security.passwords.verify_dummy", lambda password: called.append(password)
    )

    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, user.username)

    assert called == [PASSWORD]


def test_successful_login_clears_the_failure_counter(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("xoabodem")
    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, user.username, WRONG_PASSWORD)

    _login(session_factory, user.username)

    assert _reload(session_factory, user.id).failed_login_count == 0


def test_account_locks_after_repeated_failures_and_stays_locked(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Khóa tạm phải chặn cả lần thử với mật khẩu **đúng** — nếu không, nó chỉ
    làm chậm một cuộc dò chứ không dừng được nó."""
    user = user_factory("khoatam")

    for _ in range(auth_service.LOCKOUT_THRESHOLD):
        with pytest.raises(InvalidCredentialsError):
            _login(session_factory, user.username, WRONG_PASSWORD)

    reloaded = _reload(session_factory, user.id)
    assert reloaded.locked_until is not None
    assert reloaded.failed_login_count == 0

    with pytest.raises(AccountLockedError):
        _login(session_factory, user.username)

    assert ControlAuditAction.LOGIN_BLOCKED.value in _audit_actions(session_factory, user.id)


def test_lock_expires_on_its_own(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Khóa là **tạm**: người quên mật khẩu không phải chờ quản trị viên."""
    user = user_factory("hetkhoa")
    for _ in range(auth_service.LOCKOUT_THRESHOLD):
        with pytest.raises(InvalidCredentialsError):
            _login(session_factory, user.username, WRONG_PASSWORD)

    later = datetime.now(UTC) + auth_service.LOCKOUT_DURATION + timedelta(minutes=1)
    assert _login(session_factory, user.username, now=later).token


def test_disabled_account_cannot_log_in(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("vohieuhoa", is_active=False)
    with pytest.raises(InvalidCredentialsError):
        _login(session_factory, user.username)


def test_resolve_session_returns_the_principal(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("phien")
    issued = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        principal = auth_service.resolve_session(session, issued.token)
    assert principal.user_id == user.id
    assert principal.username == user.username


def test_unknown_expired_and_revoked_tokens_are_all_rejected(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("thuhoi")
    issued = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        with pytest.raises(NotAuthenticatedError):
            auth_service.resolve_session(session, "token-không-có-thật")
        with pytest.raises(NotAuthenticatedError):
            auth_service.resolve_session(
                session, issued.token, now=issued.expires_at + timedelta(seconds=1)
            )

    with control_session(session_factory) as session:
        principal = auth_service.resolve_session(session, issued.token)
        auth_service.revoke_session(session, session_id=principal.session_id, actor_user_id=user.id)

    with control_session(session_factory) as session, pytest.raises(NotAuthenticatedError):
        auth_service.resolve_session(session, issued.token)


def test_logout_leaves_a_trace(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Đăng xuất cũng là sự kiện phiên — thiếu vết thì không dựng lại được
    "phiên này sống từ lúc nào tới lúc nào" khi có sự cố."""
    user = user_factory("vet_dangxuat")
    issued = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        principal = auth_service.resolve_session(session, issued.token)
        auth_service.revoke_session(session, session_id=principal.session_id, actor_user_id=user.id)

    assert _audit_actions(session_factory, user.id)[-1] == ControlAuditAction.LOGOUT.value


def test_deactivating_a_user_kills_live_sessions_immediately(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    """Lý do tồn tại của token lưu DB thay vì JWT tự chứa (`auth_models`)."""
    user = user_factory("duoiviec")
    issued = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        reloaded.is_active = False

    with control_session(session_factory) as session, pytest.raises(NotAuthenticatedError):
        auth_service.resolve_session(session, issued.token)


def test_changing_password_revokes_every_other_session(
    session_factory: sessionmaker[Session], user_factory: UserFactory
) -> None:
    user = user_factory("doimatkhau")
    first = _login(session_factory, user.username)
    second = _login(session_factory, user.username)

    with control_session(session_factory) as session:
        kept = auth_service.resolve_session(session, second.token)
        account_service.change_own_password(
            session,
            user=account_service.find_user(session, user.username),
            current_password=PASSWORD,
            new_password="Moi#MatKhau2026",
        )
        auth_service.revoke_other_sessions(
            session, user_id=user.id, keep_session_id=kept.session_id, actor_user_id=user.id
        )

    with control_session(session_factory) as session:
        with pytest.raises(NotAuthenticatedError):
            auth_service.resolve_session(session, first.token)
        assert auth_service.resolve_session(session, second.token).user_id == user.id


def test_two_factor_is_required_enforced_and_replay_protected(
    session_factory: sessionmaker[Session], user_factory: UserFactory, secret_box: SecretBox
) -> None:
    """Vòng đời 2FA đầy đủ: bắt buộc → chưa đăng ký → đăng ký → ép mã → chống dùng lại."""
    user = user_factory("haitang", totp_required=True)

    with pytest.raises(TotpEnrollmentRequiredError):
        _login(session_factory, user.username, secret_box=secret_box)

    with control_session(session_factory) as session:
        enrolling = account_service.find_user(session, user.username)
        uri = account_service.begin_totp_enrollment(session, user=enrolling, secret_box=secret_box)
    secret = uri.split("secret=")[1].split("&")[0]

    # Chưa xác nhận thì vẫn coi như chưa đăng ký.
    with pytest.raises(TotpEnrollmentRequiredError):
        _login(session_factory, user.username, secret_box=secret_box)

    with control_session(session_factory) as session:
        account_service.confirm_totp_enrollment(
            session,
            user=account_service.find_user(session, user.username),
            code=_code(secret),
            secret_box=secret_box,
        )

    with pytest.raises(TotpRequiredError):
        _login(session_factory, user.username, secret_box=secret_box)

    moment = datetime.now(UTC) + timedelta(seconds=totp.PERIOD_SECONDS)
    code = _code(secret, moment)
    assert _login(
        session_factory, user.username, secret_box=secret_box, totp_code=code, now=moment
    ).token

    with pytest.raises(TotpCodeReusedError):
        # Dùng lại đúng mã vừa dùng: phải bị từ chối, không cấp phiên thứ hai.
        _login(
            session_factory,
            user.username,
            secret_box=secret_box,
            totp_code=code,
            now=moment,
        )


def test_secret_is_never_stored_in_readable_form(
    session_factory: sessionmaker[Session], user_factory: UserFactory, secret_box: SecretBox
) -> None:
    """Bản dump DB không được chứa bí mật TOTP (RT-05)."""
    user = user_factory("mahoa")
    with control_session(session_factory) as session:
        uri = account_service.begin_totp_enrollment(
            session, user=account_service.find_user(session, user.username), secret_box=secret_box
        )
    secret = uri.split("secret=")[1].split("&")[0]

    stored = _reload(session_factory, user.id).totp_secret_enc
    assert stored is not None
    assert secret.encode("ascii") not in stored
    assert secret_box.decrypt(stored) == secret


def _code(secret: str, moment: datetime | None = None) -> str:
    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    return generator.at(moment if moment is not None else datetime.now(UTC))
