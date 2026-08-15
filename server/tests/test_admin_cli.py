"""Lệnh quản trị tại máy chủ (`python -m ket.admin`).

Bộ test này tồn tại vì lý do rất cụ thể: trước lát 2B-1a, quy trình khôi phục
cụm là một đoạn Python **chép trong tài liệu**, không có gì kiểm. Một điểm vào có
thật chỉ hơn tài liệu khi nó được chạy — nên ở đây từng lệnh được gọi đúng cách
người vận hành sẽ gọi, kể cả đường `--password-stdin` mà installer dùng.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.admin.cli import main
from ket.kernel.auditing.control_log import ControlAuditAction, ControlAuditLog
from ket.kernel.datasets.models import User
from ket.kernel.errors import NotAuthenticatedError
from ket.kernel.persistence.session import control_session
from ket.kernel.security import auth_service, passwords
from ket.kernel.security.keystore import SecretBox
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
NEW_PASSWORD = "Moi#MatKhau2026"
UserFactory = Callable[..., User]


@pytest.fixture(autouse=True)
def cli_settings(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI đọc cấu hình từ môi trường; trỏ nó vào database test.

    Vá `get_settings` **trong module `cli`** chứ không đặt biến môi trường: đặt
    biến môi trường sẽ rò sang các test khác chạy sau trong cùng tiến trình.
    """
    monkeypatch.setattr("ket.admin.cli.get_settings", lambda: test_settings)


def _run(*argv: str, stdin: str | None = None, monkeypatch: pytest.MonkeyPatch) -> int:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(list(argv))


def _user(factory: sessionmaker[Session], username: str) -> User | None:
    with control_session(factory) as session:
        return session.scalars(select(User).where(User.username == username)).one_or_none()


def test_create_user_makes_a_working_account(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    username = "cli_tao_moi"
    code = _run(
        "create-user", username, "--password-stdin", stdin=f"{PASSWORD}\n", monkeypatch=monkeypatch
    )

    assert code == 0
    created = _user(session_factory, username)
    assert created is not None
    assert created.must_change_password is True
    assert passwords.verify_password(created.password_hash, PASSWORD)


def test_create_user_records_the_event(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tạo tài khoản từ CLI cũng phải để lại vết — đây là đường phá-kính, tức là
    đúng đường mà một cuộc điều tra sẽ hỏi tới đầu tiên."""
    username = "cli_co_vet"
    _run(
        "create-user", username, "--password-stdin", stdin=f"{PASSWORD}\n", monkeypatch=monkeypatch
    )

    created = _user(session_factory, username)
    assert created is not None
    with control_session(session_factory) as session:
        actions = list(
            session.scalars(
                select(ControlAuditLog.action).where(ControlAuditLog.subject_user_id == created.id)
            )
        )
    assert actions == [ControlAuditAction.USER_CREATED.value]


def test_create_user_refuses_a_duplicate_name_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lỗi nghiệp vụ ở CLI là **một dòng**, không phải 30 dòng stack."""
    username = "cli_trung_ten"
    _run(
        "create-user", username, "--password-stdin", stdin=f"{PASSWORD}\n", monkeypatch=monkeypatch
    )
    capsys.readouterr()

    code = _run(
        "create-user", username, "--password-stdin", stdin=f"{PASSWORD}\n", monkeypatch=monkeypatch
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "auth.user_already_exists" in captured.err
    assert "Traceback" not in captured.err


def test_create_user_enforces_the_password_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Đường CLI **không** được là cửa sau để đặt mật khẩu yếu."""
    code = _run(
        "create-user", "cli_yeu", "--password-stdin", stdin="ngan\n", monkeypatch=monkeypatch
    )

    assert code == 1
    assert "auth.password_too_weak" in capsys.readouterr().err


def test_reset_password_lets_the_user_log_in_again(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đường phá-kính: quản trị viên tự khóa mình ra ngoài vẫn mở lại được."""
    user = user_factory("cli_datlai")

    code = _run(
        "reset-password",
        user.username,
        "--password-stdin",
        stdin=f"{NEW_PASSWORD}\n",
        monkeypatch=monkeypatch,
    )

    assert code == 0
    issued = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=NEW_PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=auth_service.LOCKOUT_DURATION,
    )
    assert issued.must_change_password is True


def test_reset_password_clears_a_lockout(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đặt lại mật khẩu phải mở khóa luôn: người bị khóa chính là người gọi hỗ trợ."""
    user = user_factory("cli_mokhoa")
    for _ in range(auth_service.LOCKOUT_THRESHOLD):
        with pytest.raises(Exception, match="Sai tên đăng nhập"):
            auth_service.authenticate(
                session_factory,
                username=user.username,
                password="Sai#MatKhau2026",
                secret_box_provider=_no_key_needed,
                session_ttl=auth_service.LOCKOUT_DURATION,
            )

    _run(
        "reset-password",
        user.username,
        "--password-stdin",
        stdin=f"{NEW_PASSWORD}\n",
        monkeypatch=monkeypatch,
    )

    with control_session(session_factory) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        assert reloaded.locked_until is None
        assert reloaded.failed_login_count == 0


def test_reset_password_revokes_every_open_session(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đặt lại mật khẩu được gọi đúng lúc nghi tài khoản bị chiếm.

    Để token cũ sống thêm 12 giờ thì việc đặt lại mật khẩu không đuổi được ai —
    và đường HTTP `change-password` thì có thu hồi, nên hai đường cùng nghĩa mà
    hành xử ngược nhau là cái bẫy tệ nhất.
    """
    user = user_factory("cli_duoi_phien")
    issued = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=auth_service.LOCKOUT_DURATION,
    )

    _run(
        "reset-password",
        user.username,
        "--password-stdin",
        stdin=f"{NEW_PASSWORD}\n",
        monkeypatch=monkeypatch,
    )

    with control_session(session_factory) as session, pytest.raises(NotAuthenticatedError):
        auth_service.resolve_session(session, issued.token)


def test_reset_totp_unlocks_a_user_who_lost_the_authenticator(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đường thoát cho ngõ cụt: bắt buộc 2FA nhưng chưa/không còn đăng ký được.

    Không đăng nhập được thì cũng không gọi được `/auth/totp/enroll`, nên nếu
    không có lệnh này thì tài khoản chết hẳn — kể cả tài khoản quản trị duy nhất.
    """
    user = user_factory("cli_mat_dienthoai", totp_required=True)

    assert _run("reset-totp", user.username, monkeypatch=monkeypatch) == 0

    with control_session(session_factory) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        assert reloaded.totp_required is False
        assert reloaded.totp_secret_enc is None
        assert reloaded.totp_enrolled_at is None

    assert auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=auth_service.LOCKOUT_DURATION,
    ).token


def test_reset_password_names_a_missing_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        "reset-password",
        "khong_ton_tai_bao_gio",
        "--password-stdin",
        stdin=f"{NEW_PASSWORD}\n",
        monkeypatch=monkeypatch,
    )

    assert code == 1
    assert "auth.user_not_found" in capsys.readouterr().err


def test_ensure_cluster_runs_and_is_repeatable(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lệnh khôi phục cụm phải chạy được **từ CLI**, không chỉ từ đoạn mã trong tài liệu."""
    assert _run("ensure-cluster", monkeypatch=monkeypatch) == 0
    assert _run("ensure-cluster", monkeypatch=monkeypatch) == 0


def _no_key_needed() -> SecretBox:
    raise AssertionError("tài khoản không bật 2FA thì không được chạm tới khóa mã hóa")
