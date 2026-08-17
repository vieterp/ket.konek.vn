"""Lệnh quản trị tại máy chủ (`python -m ket.admin`).

Bộ test này tồn tại vì lý do rất cụ thể: trước lát 2B-1a, quy trình khôi phục
cụm là một đoạn Python **chép trong tài liệu**, không có gì kiểm. Một điểm vào có
thật chỉ hơn tài liệu khi nó được chạy — nên ở đây từng lệnh được gọi đúng cách
người vận hành sẽ gọi, kể cả đường `--password-stdin` mà installer dùng.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.admin.cli import main
from ket.kernel.auditing.control_log import ControlAuditAction, ControlAuditLog
from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import NotAuthenticatedError
from ket.kernel.idempotency.models import IdempotencyKey
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import control_session, create_session_factory, dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import auth_service, passwords
from ket.kernel.security.auth_models import AuthSession, token_digest
from ket.kernel.security.authorization import resolve_access
from ket.kernel.security.keystore import SecretBox
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
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


def test_grant_role_opens_a_brand_new_dataset_for_the_first_user(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đường phá-kính thật sự cần thiết: chưa ai có `system.role.edit`.

    Không có lệnh này thì một dữ liệu kế toán vừa tạo là cái hộp không ai mở
    được — gán vai trò qua HTTP đòi quyền mà chưa ai được cấp.
    """
    user = user_factory("cli_gan_vaitro")

    assert (
        _run(
            "grant-role",
            user.username,
            "--dataset",
            dataset_alpha.code,
            "--role",
            "admin",
            monkeypatch=monkeypatch,
        )
        == 0
    )

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=AuditContext(user_id=user.id),
    ) as session:
        access = resolve_access(session, user_id=user.id)
    assert permission_code(SYSTEM_MODULE, "role", Action.EDIT) in access.permissions

    # Vai trò `admin` mang quyền nhạy cảm → cờ 2FA phải bật, kể cả qua CLI.
    reloaded = _user(session_factory, user.username)
    assert reloaded is not None
    assert reloaded.totp_required is True


def test_grant_role_refuses_an_unknown_role_with_a_readable_message(
    dataset_alpha: DatasetRef, user_factory: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Người đọc là quản trị viên máy chủ — một dòng, không phải 30 dòng stack."""
    user = user_factory("cli_vaitro_la")
    assert (
        _run(
            "grant-role",
            user.username,
            "--dataset",
            dataset_alpha.code,
            "--role",
            "khong_ton_tai",
            monkeypatch=monkeypatch,
        )
        == 1
    )


def test_grant_branch_puts_a_user_inside_the_rls_scope(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chưa gán chi nhánh = không thấy dòng nào — nên đây là bước bắt buộc."""
    user = user_factory("cli_gan_chinhanh")
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=user.id, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Branch.id).where(Branch.code == "CN_CLI")) is None:
            BranchService(session).create(code="CN_CLI", name="Chi nhánh CLI")

    _run(
        "grant-role",
        user.username,
        "--dataset",
        dataset_alpha.code,
        "--role",
        "admin",
        monkeypatch=monkeypatch,
    )
    assert (
        _run(
            "grant-branch",
            user.username,
            "--dataset",
            dataset_alpha.code,
            "--branch",
            "CN_CLI",
            monkeypatch=monkeypatch,
        )
        == 0
    )

    with dataset_session(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        branch_ids=(),
        audit=AuditContext(user_id=user.id),
    ) as session:
        access = resolve_access(session, user_id=user.id)
    assert len(access.branch_ids) == 1


def test_prune_sessions_removes_dead_sessions_and_keeps_live_ones(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dọn phiên đã chết, giữ phiên đang dùng.

    Bảng `auth_sessions` chỉ tăng, nên nó cần một đường dọn; nhưng đường dọn mà
    xóa nhầm phiên đang sống sẽ đuổi cả văn phòng ra ngoài giữa giờ làm.
    """
    user = user_factory("cli_don_phien")
    live = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=timedelta(hours=12),
    )

    # Một phiên đã hết hạn từ lâu, dựng bằng cách đẩy `expires_at` về quá khứ.
    stale = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=timedelta(hours=12),
    )
    long_ago = datetime.now(UTC) - timedelta(days=400)
    with control_session(session_factory) as session:
        row = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(stale.token))
        ).one()
        row.expires_at = long_ago

    code = _run("prune-sessions", "--retention-days", "30", monkeypatch=monkeypatch)

    assert code == 0
    with control_session(session_factory) as session:
        remaining = session.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all()
        tokens = {bytes(row.token_hash) for row in remaining}

    assert token_digest(live.token) in tokens, "phiên đang dùng bị xóa nhầm"
    assert token_digest(stale.token) not in tokens, "phiên đã hết hạn vẫn còn"


def test_prune_sessions_needs_the_owner_role(test_settings: Settings) -> None:
    """Vai trò runtime cố ý **không** có `DELETE` trên `auth_sessions` (RT-02).

    Đây là lý do lệnh dọn nằm ở CLI chứ không ở một endpoint: nếu đường API xóa
    được dấu vết đăng nhập thì một lỗ hổng ở tầng đó cũng xóa được.
    """
    app_only = create_engine(test_settings.database_url)
    try:
        factory = create_session_factory(app_only)
        with pytest.raises(ProgrammingError) as failure, control_session(factory) as session:
            auth_service.prune_expired_sessions(session, retention=timedelta(days=0))
    finally:
        app_only.dispose()

    assert "permission denied" in str(failure.value).lower()


def test_prune_sessions_keeps_a_recently_expired_session(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cửa sổ lưu trữ phải có tác dụng thật.

    Phiên hết hạn **trong** cửa sổ vẫn là nguồn điều tra: "tài khoản đó đăng
    nhập từ máy nào sáng nay" là câu hỏi được đặt ra sau khi chuyện đã xảy ra.
    Bỏ cửa sổ (`cutoff = now`) thì mọi phiên vừa hết hạn biến mất ngay.
    """
    user = user_factory("cli_giu_moi_het_han")
    issued = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=timedelta(hours=12),
    )
    with control_session(session_factory) as session:
        row = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one()
        row.expires_at = datetime.now(UTC) - timedelta(days=2)

    assert _run("prune-sessions", "--retention-days", "30", monkeypatch=monkeypatch) == 0

    with control_session(session_factory) as session:
        still_there = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one_or_none()

    assert still_there is not None, "phiên hết hạn 2 ngày trước bị dọn dù cửa sổ là 30 ngày"


def test_prune_sessions_removes_a_long_revoked_session_that_has_not_expired_yet(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nhánh thứ hai của mệnh đề `WHERE` — phiên **bị thu hồi** từ lâu.

    Nó vẫn còn hạn trên giấy tờ (`expires_at` ở tương lai) nhưng đã chết từ lúc
    bị thu hồi. Chỉ lọc theo `expires_at` sẽ giữ lại đúng những dòng vô dụng
    nhất, và test cũ không canh nhánh này.
    """
    user = user_factory("cli_thu_hoi_lau")
    issued = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=timedelta(days=3650),
    )
    long_ago = datetime.now(UTC) - timedelta(days=400)
    with control_session(session_factory) as session:
        row = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one()
        row.revoked_at = long_ago

    assert _run("prune-sessions", "--retention-days", "30", monkeypatch=monkeypatch) == 0

    with control_session(session_factory) as session:
        remaining = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one_or_none()

    assert remaining is None, "phiên bị thu hồi 400 ngày trước vẫn còn vì `expires_at` ở tương lai"


def test_prune_sessions_records_who_ran_it(
    session_factory: sessionmaker[Session],
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Xóa lịch sử phiên phải tự nó là một sự kiện kiểm toán.

    `auth_sessions` là nguồn trả lời "ai đăng nhập từ máy nào, lúc mấy giờ" khi
    có tranh chấp; một lần dọn không để lại gì là một khoảng trống không giải
    thích được — đúng loại khoảng trống mà nhật ký chỉ-thêm sinh ra để lấp.
    """
    user = user_factory("cli_don_co_vet")
    issued = auth_service.authenticate(
        session_factory,
        username=user.username,
        password=PASSWORD,
        secret_box_provider=_no_key_needed,
        session_ttl=timedelta(hours=12),
    )
    with control_session(session_factory) as session:
        row = session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_digest(issued.token))
        ).one()
        row.expires_at = datetime.now(UTC) - timedelta(days=400)

    _run("prune-sessions", "--retention-days", "30", monkeypatch=monkeypatch)

    with control_session(session_factory) as session:
        entry = session.scalars(
            select(ControlAuditLog)
            .where(ControlAuditLog.action == ControlAuditAction.SESSIONS_PRUNED.value)
            .order_by(ControlAuditLog.id.desc())
        ).first()

    assert entry is not None, "dọn phiên không để lại vết nào"
    assert entry.client_info == "ket.admin"
    assert (entry.new_values or {})["retention_days"] == 30
    assert int((entry.new_values or {})["removed_sessions"]) >= 1


def test_prune_sessions_refuses_a_negative_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--retention-days -3650` đẩy mốc cắt về **tương lai** → xóa cả phiên sống.

    `type=int` trần cho giá trị đó đi qua, và một lệnh dọn dẹp biến thành lệnh
    đuổi cả văn phòng ra khỏi hệ thống.
    """
    with pytest.raises(SystemExit):
        _run("prune-sessions", "--retention-days", "-3650", monkeypatch=monkeypatch)


def test_prune_idempotency_keys_clears_expired_keys_in_every_dataset(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Khóa hết hạn không còn chống trùng cho gì — nhưng bảng thì chỉ tăng."""
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        session.add(
            IdempotencyKey(
                route_key="POST /test/cli",
                idempotency_key="khoa-cli-het-han",
                user_id=1,
                request_fingerprint="0" * 64,
                result_type="branches",
                result_id="1",
                expires_at=datetime.now(UTC) - timedelta(days=2),
            )
        )

    assert _run("prune-idempotency-keys", monkeypatch=monkeypatch) == 0

    with unit_of_work(session_factory, scope) as session:
        left = session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.idempotency_key == "khoa-cli-het-han")
        )

    assert left is None
