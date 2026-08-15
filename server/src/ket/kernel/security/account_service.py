"""Vòng đời tài khoản: tạo, đổi/đặt lại mật khẩu, đăng ký 2FA (FR-SYS-070/075).

Tách khỏi `auth_service` theo trục thời gian chứ không theo trục kỹ thuật:
`auth_service` là thứ chạy **mỗi lần** đăng nhập và mỗi request; tệp này là thứ
chạy **thỉnh thoảng**, khi tài khoản thay đổi. Hai nhóm có yêu cầu rất khác nhau
về chi phí và về việc ghi vết, và trộn chung sẽ khiến đường nóng phải mang theo
mã của đường nguội.

Mọi hàm ở đây nhận `Session` đã mở transaction và **không** tự commit — người mở
transaction cũng là người đóng (`persistence/unit_of_work.py`). Nhờ vậy dòng
`control_audit_log` luôn đi cùng chuyến với thay đổi nó mô tả.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.auditing.control_log import ControlAuditAction, record_control_action
from ket.kernel.datasets.models import User
from ket.kernel.errors import (
    InvalidCredentialsError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from ket.kernel.security import passwords, totp
from ket.kernel.security.keystore import SecretBox


def find_user(session: Session, username: str) -> User:
    """Tìm theo tên đăng nhập; không có → `UserNotFoundError`.

    Chỉ dùng cho đường **quản trị** (CLI, màn hình người dùng). Đường đăng nhập
    không được gọi hàm này: nó phân biệt "không có tài khoản" với "sai mật
    khẩu", đúng thứ `auth_service` cố tình gộp lại.
    """
    user = session.scalars(select(User).where(User.username == username)).one_or_none()
    if user is None:
        raise UserNotFoundError("Không có người dùng với tên đăng nhập này", username=username)
    return user


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    email: str | None = None,
    locale: str = "vi",
    must_change_password: bool = True,
    actor_user_id: int | None = None,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> User:
    """Tạo danh tính toàn cục mới.

    `must_change_password=True` mặc định: mật khẩu đầu tiên do người khác đặt và
    đi qua một kênh nào đó (lời nói, tin nhắn) — nó không còn là bí mật của riêng
    chủ tài khoản kể từ giây phút được đặt.
    """
    passwords.validate_policy(password, username=username)
    if session.scalars(select(User.id).where(User.username == username)).one_or_none() is not None:
        raise UserAlreadyExistsError("Tên đăng nhập đã tồn tại", username=username)

    user = User(
        username=username,
        password_hash=passwords.hash_password(password),
        email=email,
        locale=locale,
        must_change_password=must_change_password,
    )
    session.add(user)
    session.flush()

    record_control_action(
        session,
        action=ControlAuditAction.USER_CREATED,
        actor_user_id=actor_user_id,
        subject_user_id=user.id,
        subject_label=username,
        new_values={"username": username, "locale": locale, "email": email},
        correlation_id=correlation_id,
        client_info=client_info,
    )
    return user


def reset_password(
    session: Session,
    *,
    user: User,
    new_password: str,
    actor_user_id: int | None = None,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> None:
    """Đặt lại mật khẩu **không cần biết mật khẩu cũ** (FR-SYS-075).

    Đường của quản trị viên và của CLI phá-kính. Luôn bật
    `must_change_password`: người đặt lại biết mật khẩu mới, nên nó chỉ được
    sống tới lần đăng nhập kế tiếp.

    Không thu hồi phiên ở đây — người gọi quyết định, vì hai ngữ cảnh khác nhau:
    quản trị viên đặt lại cho người **khác** thì phải đuổi hết phiên cũ, còn CLI
    chạy lúc chưa có ai đăng nhập thì không có gì để đuổi.
    """
    passwords.validate_policy(new_password, username=user.username)
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None

    record_control_action(
        session,
        action=ControlAuditAction.PASSWORD_RESET,
        actor_user_id=actor_user_id,
        subject_user_id=user.id,
        subject_label=user.username,
        correlation_id=correlation_id,
        client_info=client_info,
    )


def change_own_password(
    session: Session,
    *,
    user: User,
    current_password: str,
    new_password: str,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> None:
    """Chủ tài khoản tự đổi mật khẩu — phải chứng minh mật khẩu hiện tại.

    Chứng minh lại dù đã có phiên hợp lệ: một máy trạm bỏ quên không khóa màn
    hình là kịch bản thật trong văn phòng, và đổi mật khẩu là thao tác chiếm
    được tài khoản vĩnh viễn.
    """
    if not passwords.verify_password(user.password_hash, current_password):
        raise InvalidCredentialsError("Mật khẩu hiện tại không đúng")

    passwords.validate_policy(new_password, username=user.username)
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False

    record_control_action(
        session,
        action=ControlAuditAction.PASSWORD_CHANGED,
        actor_user_id=user.id,
        subject_user_id=user.id,
        subject_label=user.username,
        correlation_id=correlation_id,
        client_info=client_info,
    )


def begin_totp_enrollment(session: Session, *, user: User, secret_box: SecretBox) -> str:
    """Sinh bí mật mới, lưu **đã mã hóa**, trả về URI `otpauth://` dựng mã QR.

    Bí mật được lưu ngay nhưng `totp_enrolled_at` vẫn `NULL` — tài khoản chưa
    được coi là đã đăng ký cho tới khi `confirm_totp_enrollment` chứng minh
    thiết bị sinh đúng mã. Gọi lại hàm này sẽ **thay** bí mật chưa xác nhận, nên
    một lần quét hỏng không khóa ai ra ngoài.

    Chuỗi trả về chứa bí mật dạng rõ: chỉ đi thẳng tới người đang đăng ký, không
    ghi log, không ghi nhật ký.
    """
    secret = totp.generate_secret()
    user.totp_secret_enc = secret_box.encrypt(secret)
    user.totp_enrolled_at = None
    user.totp_last_counter = None
    # Chưa ghi `control_audit_log`: chưa có gì thay đổi về mặt an ninh cho tới
    # khi xác nhận. Ghi ở đây sẽ đẻ ra một dòng mỗi lần người dùng mở lại màn
    # hình đăng ký.
    return totp.provisioning_uri(secret, username=user.username)


def confirm_totp_enrollment(
    session: Session,
    *,
    user: User,
    code: str,
    secret_box: SecretBox,
    now: datetime | None = None,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> None:
    """Xác nhận thiết bị sinh mã hoạt động; từ đây 2FA mới có hiệu lực."""
    moment = now if now is not None else datetime.now(UTC)
    if user.totp_secret_enc is None:
        raise UserNotFoundError(
            "Chưa bắt đầu đăng ký xác thực hai lớp cho tài khoản này",
            username=user.username,
        )

    secret = secret_box.decrypt(user.totp_secret_enc)
    user.totp_last_counter = totp.verify_code(
        secret, code, last_counter=user.totp_last_counter, now=moment
    )
    user.totp_enrolled_at = moment

    record_control_action(
        session,
        action=ControlAuditAction.TOTP_ENROLLED,
        actor_user_id=user.id,
        subject_user_id=user.id,
        subject_label=user.username,
        correlation_id=correlation_id,
        client_info=client_info,
    )


def disable_totp(
    session: Session,
    *,
    user: User,
    actor_user_id: int | None = None,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> None:
    """Gỡ 2FA (mất điện thoại). Xóa sạch cả ba cột, không chỉ cờ xác nhận.

    Giữ lại bí mật cũ để "bật lại cho nhanh" là giữ một bí mật mà chủ tài khoản
    không còn kiểm soát thiết bị chứa nó.

    **Không** đụng tới `totp_required`: cờ đó do vai trò quyết định (lát 2B-1b).
    Nếu tài khoản vẫn thuộc diện bắt buộc, lần đăng nhập kế tiếp sẽ dừng ở
    `TotpEnrollmentRequiredError` và buộc đăng ký thiết bị mới — đúng ý muốn.
    """
    user.totp_secret_enc = None
    user.totp_enrolled_at = None
    user.totp_last_counter = None

    record_control_action(
        session,
        action=ControlAuditAction.TOTP_DISABLED,
        actor_user_id=actor_user_id if actor_user_id is not None else user.id,
        subject_user_id=user.id,
        subject_label=user.username,
        correlation_id=correlation_id,
        client_info=client_info,
    )
