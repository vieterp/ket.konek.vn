"""Đăng nhập và vòng đời phiên (bước 7 của phase 2).

Ba quyết định định hình tệp này:

1. **Đường thất bại cũng phải để lại vết, nên nó phải commit.** Đăng nhập sai là
   sự kiện an ninh: bộ đếm khóa tài khoản và dòng `control_audit_log` chỉ có giá
   trị nếu chúng sống sót. Vì vậy `authenticate` **dựng** lỗi rồi ném **sau khi**
   transaction đã commit, chứ không ném từ trong lòng transaction — ném từ trong
   sẽ rollback đúng cái vết vừa ghi.
2. **Chi phí như nhau ở mọi nhánh sai.** Tài khoản không tồn tại và tài khoản bị
   vô hiệu hóa đều chạy qua `passwords.verify_dummy`; thiếu bước đó thì thời
   gian phản hồi tự nó liệt kê được danh sách nhân sự.
3. **Khóa mã hóa nạp trễ.** Chỉ tài khoản bật 2FA mới cần khóa app, nên hàm nhận
   một *provider* thay vì một `SecretBox`: bản cài chưa cấu hình keystore vẫn
   đăng nhập được cho những tài khoản không bật 2FA (ADR-019).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.control_log import ControlAuditAction, record_control_action
from ket.kernel.datasets.models import User
from ket.kernel.errors import (
    AccountLockedError,
    DomainError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    TotpEnrollmentRequiredError,
    TotpRequiredError,
)
from ket.kernel.persistence.session import control_session
from ket.kernel.security import passwords, totp
from ket.kernel.security.auth_models import AuthSession, new_session_token, token_digest
from ket.kernel.security.keystore import SecretBox

SecretBoxProvider = Callable[[], SecretBox]
"""Nạp khóa app khi — và chỉ khi — có bí mật cần mở."""

LOCKOUT_THRESHOLD: Final[int] = 10
"""Số lần sai liên tiếp trước khi khóa tạm.

10 chứ không phải 3: người dùng thật gõ sai nhiều hơn ta tưởng (bàn phím tiếng
Việt, Caps Lock, mật khẩu vừa đổi ở máy khác), còn kẻ dò mật khẩu thì 3 hay 10
đều không đáng kể — thứ chặn được nó là **thời gian khóa**, không phải ngưỡng."""

LOCKOUT_DURATION: Final[timedelta] = timedelta(minutes=15)
"""Đủ dài để một cuộc dò tự động trở nên vô vọng, đủ ngắn để một kế toán viên
quên mật khẩu không phải chờ quản trị viên."""

LAST_SEEN_REFRESH: Final[timedelta] = timedelta(minutes=5)
"""Ngưỡng ghi lại `last_seen_at`. Không có ngưỡng thì mọi request đọc đều kéo
theo một request ghi."""


@dataclass(frozen=True)
class Principal:
    """Người dùng đứng sau một request đã xác thực.

    Chưa có vai trò/quyền: chúng là thứ **per-dataset** và chỉ phân giải được sau
    khi request chọn dữ liệu kế toán (lát 2B-1b).
    """

    user_id: int
    username: str
    locale: str
    session_id: int
    must_change_password: bool
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    """Kết quả đăng nhập thành công. `token` chỉ tồn tại ở đây và trong phản hồi."""

    token: str
    expires_at: datetime
    user_id: int
    must_change_password: bool


@dataclass
class _LoginOutcome:
    """Kết quả tạm của một lần thử đăng nhập, chờ transaction commit xong.

    Lỗi được **mang ra ngoài** thay vì ném ngay: xem quyết định 1 ở đầu tệp.
    """

    issued: IssuedSession | None = None
    error: DomainError | None = None


def authenticate(
    factory: sessionmaker[Session],
    *,
    username: str,
    password: str,
    secret_box_provider: SecretBoxProvider,
    session_ttl: timedelta,
    totp_code: str | None = None,
    client_info: str | None = None,
    correlation_id: UUID | None = None,
    now: datetime | None = None,
) -> IssuedSession:
    """Kiểm danh tính và cấp phiên. Sai → ném lỗi **sau khi** đã ghi vết."""
    moment = now if now is not None else datetime.now(UTC)
    with control_session(factory) as session:
        outcome = _evaluate_login(
            session,
            username=username,
            password=password,
            totp_code=totp_code,
            secret_box_provider=secret_box_provider,
            session_ttl=session_ttl,
            client_info=client_info,
            correlation_id=correlation_id,
            now=moment,
        )
    if outcome.error is not None:
        raise outcome.error
    if outcome.issued is None:  # pragma: no cover - _evaluate_login luôn đặt một trong hai
        raise InvalidCredentialsError("Không cấp được phiên đăng nhập")
    return outcome.issued


def _evaluate_login(
    session: Session,
    *,
    username: str,
    password: str,
    totp_code: str | None,
    secret_box_provider: SecretBoxProvider,
    session_ttl: timedelta,
    client_info: str | None,
    correlation_id: UUID | None,
    now: datetime,
) -> _LoginOutcome:
    """Toàn bộ luật đăng nhập, chạy trong transaction của người gọi."""

    def audit(action: ControlAuditAction, user: User | None = None) -> None:
        record_control_action(
            session,
            action=action,
            actor_user_id=user.id if user is not None else None,
            subject_user_id=user.id if user is not None else None,
            subject_label=username,
            correlation_id=correlation_id,
            client_info=client_info,
        )

    # `FOR UPDATE`: hai cơ chế của hàm này là **đọc–sửa–ghi** trên cùng một dòng
    # `users` — bộ đếm khóa tài khoản và bước thời gian TOTP đã dùng. Không khóa
    # dòng thì hai lần đăng nhập song song cùng đọc giá trị cũ và cùng ghi đè
    # nhau, nên:
    #
    #   * 10 lần sai **đồng thời** để lại `failed_login_count = 1` thay vì khóa
    #     tài khoản — hàng phòng thủ duy nhất chống dò mật khẩu biến mất với
    #     người mở nhiều kết nối;
    #   * cùng một mã TOTP dùng song song cấp được nhiều phiên — chống phát lại
    #     vô hiệu, và đó chính là thứ `totp_last_counter` sinh ra để chặn.
    #
    # Đo được cả hai trên cụm thật, nên đây không phải rủi ro lý thuyết. Giá phải
    # trả: các lần đăng nhập của **cùng một** người dùng nối đuôi nhau. Đăng nhập
    # là thao tác thưa (một lần mỗi buổi làm việc) nên không nằm trên đường nóng
    # nào; người dùng khác nhau khóa dòng khác nhau, không chờ nhau.
    user = session.scalars(
        select(User).where(User.username == username).with_for_update()
    ).one_or_none()

    if user is None:
        passwords.verify_dummy(password)
        audit(ControlAuditAction.LOGIN_FAILED)
        return _LoginOutcome(error=InvalidCredentialsError("Sai tên đăng nhập hoặc mật khẩu"))

    if user.locked_until is not None and user.locked_until > now:
        audit(ControlAuditAction.LOGIN_BLOCKED, user)
        return _LoginOutcome(
            error=AccountLockedError(
                "Tài khoản tạm khóa do đăng nhập sai nhiều lần — thử lại sau ít phút",
                locked_until=user.locked_until.isoformat(),
            )
        )

    if not user.is_active:
        # Vẫn băm: tài khoản bị vô hiệu hóa không được trả lời nhanh hơn tài
        # khoản đang hoạt động, nếu không "ai đã nghỉ việc" trở thành thông tin
        # đọc được từ bên ngoài.
        passwords.verify_dummy(password)
        audit(ControlAuditAction.LOGIN_FAILED, user)
        return _LoginOutcome(error=InvalidCredentialsError("Sai tên đăng nhập hoặc mật khẩu"))

    if not passwords.verify_password(user.password_hash, password):
        _register_failure(user, now)
        audit(ControlAuditAction.LOGIN_FAILED, user)
        return _LoginOutcome(error=InvalidCredentialsError("Sai tên đăng nhập hoặc mật khẩu"))

    if user.totp_required:
        outcome = _check_second_factor(
            user,
            totp_code=totp_code,
            secret_box_provider=secret_box_provider,
            now=now,
        )
        if outcome is not None:
            # `TotpRequiredError` là bước giữa của một lần đăng nhập bình thường
            # (client chưa kịp hỏi mã), không phải một lần thử sai — không tăng
            # bộ đếm khóa, không ghi `login_failed`.
            if not isinstance(outcome, TotpRequiredError):
                _register_failure(user, now)
                audit(ControlAuditAction.LOGIN_FAILED, user)
            return _LoginOutcome(error=outcome)

    if passwords.needs_rehash(user.password_hash):
        # Lần duy nhất có mật khẩu dạng rõ trong tay — nâng tham số băm mà không
        # bắt ai đổi mật khẩu.
        user.password_hash = passwords.hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None

    issued = _issue_session(
        session, user, session_ttl=session_ttl, client_info=client_info, now=now
    )
    audit(ControlAuditAction.LOGIN_SUCCEEDED, user)
    return _LoginOutcome(issued=issued)


def _check_second_factor(
    user: User,
    *,
    totp_code: str | None,
    secret_box_provider: SecretBoxProvider,
    now: datetime,
) -> DomainError | None:
    """Kiểm lớp thứ hai. Trả `None` khi đạt, trả lỗi khi không — không ném.

    Không ném để người gọi quyết định việc ghi vết và tăng bộ đếm; ném từ đây sẽ
    kéo cả transaction (gồm vết) xuống.
    """
    if user.totp_secret_enc is None or user.totp_enrolled_at is None:
        return TotpEnrollmentRequiredError(
            "Tài khoản bắt buộc xác thực hai lớp nhưng chưa đăng ký thiết bị sinh mã"
        )
    if not totp_code:
        return TotpRequiredError("Cần mã xác thực hai lớp")

    secret = secret_box_provider().decrypt(user.totp_secret_enc)
    try:
        counter = totp.verify_code(secret, totp_code, last_counter=user.totp_last_counter, now=now)
    except DomainError as exc:
        return exc
    user.totp_last_counter = counter
    return None


def _register_failure(user: User, now: datetime) -> None:
    """Tăng bộ đếm sai; chạm ngưỡng thì khóa tạm và đặt lại bộ đếm.

    Đặt lại bộ đếm khi khóa là có chủ đích: hết thời gian khóa thì người dùng
    được một loạt lần thử mới, thay vì bị khóa lại ngay ở lần sai kế tiếp.
    """
    user.failed_login_count += 1
    if user.failed_login_count >= LOCKOUT_THRESHOLD:
        user.locked_until = now + LOCKOUT_DURATION
        user.failed_login_count = 0


def _issue_session(
    session: Session,
    user: User,
    *,
    session_ttl: timedelta,
    client_info: str | None,
    now: datetime,
) -> IssuedSession:
    """Sinh token, lưu **băm** của nó, trả token dạng rõ đúng một lần."""
    token = new_session_token()
    expires_at = now + session_ttl
    row = AuthSession(
        token_hash=token_digest(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        client_info=client_info,
    )
    session.add(row)
    session.flush()
    return IssuedSession(
        token=token,
        expires_at=expires_at,
        user_id=user.id,
        must_change_password=user.must_change_password,
    )


def resolve_session(session: Session, token: str, *, now: datetime | None = None) -> Principal:
    """Token → `Principal`. Mọi lý do không hợp lệ đều ném `NotAuthenticatedError`.

    Tra bằng **băm** của token: bảng không bao giờ chứa token dùng được, nên một
    lần đọc trộm `auth_sessions` không đổi được thành quyền đăng nhập.
    """
    moment = now if now is not None else datetime.now(UTC)
    row = session.scalars(
        select(AuthSession).where(AuthSession.token_hash == token_digest(token))
    ).one_or_none()

    if row is None or row.revoked_at is not None or row.expires_at <= moment:
        raise NotAuthenticatedError("Phiên đăng nhập không hợp lệ hoặc đã hết hạn")

    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        # Vô hiệu hóa tài khoản phải có hiệu lực **ngay** với phiên đang mở —
        # đó là toàn bộ lý do phiên nằm trong DB thay vì trong một JWT tự chứa.
        raise NotAuthenticatedError("Phiên đăng nhập không hợp lệ hoặc đã hết hạn")

    if moment - row.last_seen_at >= LAST_SEEN_REFRESH:
        row.last_seen_at = moment

    return Principal(
        user_id=user.id,
        username=user.username,
        locale=user.locale,
        session_id=row.id,
        must_change_password=user.must_change_password,
        expires_at=row.expires_at,
    )


def revoke_session(
    session: Session,
    *,
    session_id: int,
    actor_user_id: int,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
    now: datetime | None = None,
) -> None:
    """Đăng xuất: đánh dấu thu hồi, không xóa dòng (xem `AuthSession.revoked_at`)."""
    moment = now if now is not None else datetime.now(UTC)
    row = session.get(AuthSession, session_id)
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = moment
    record_control_action(
        session,
        action=ControlAuditAction.LOGOUT,
        actor_user_id=actor_user_id,
        subject_user_id=row.user_id,
        correlation_id=correlation_id,
        client_info=client_info,
    )


def revoke_other_sessions(
    session: Session,
    *,
    user_id: int,
    keep_session_id: int | None,
    actor_user_id: int,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
    now: datetime | None = None,
) -> int:
    """Thu hồi mọi phiên còn sống của một người dùng, trừ phiên đang giữ.

    Gọi sau khi đổi mật khẩu: mật khẩu cũ có thể đã lộ, và một phiên mở sẵn trên
    máy khác vẫn dùng được nếu không đuổi nó ra. Trả về số phiên đã thu hồi.
    """
    moment = now if now is not None else datetime.now(UTC)
    statement = (
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > moment,
        )
        .values(revoked_at=moment)
    )
    if keep_session_id is not None:
        statement = statement.where(AuthSession.id != keep_session_id)

    # `rowcount` chứ không phải "SELECT các id rồi UPDATE theo danh sách": giữa
    # hai câu lệnh đó, một lần đăng nhập song song sẽ tạo ra phiên **không** nằm
    # trong danh sách và sống sót qua lần đổi mật khẩu — đúng phiên mà thao tác
    # này sinh ra để đuổi. `Session.execute` khai kiểu trả về là `Result`; DML
    # thì luôn là `CursorResult`, nơi `rowcount` có thật.
    result = cast("CursorResult[Any]", session.execute(statement))
    revoked = result.rowcount
    if revoked:
        record_control_action(
            session,
            action=ControlAuditAction.SESSION_REVOKED,
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            new_values={"revoked_sessions": revoked},
            correlation_id=correlation_id,
            client_info=client_info,
        )
    return revoked
