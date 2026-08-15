"""Endpoint xác thực (`/api/v1/auth`) — bước 7 của phase 2.

Đây là nhóm endpoint **duy nhất** chạy trước khi chọn dữ liệu kế toán, nên nó
làm việc trên schema điều khiển (`control_session`) và không đặt `search_path`
hay vai trò dataset nào. Mọi nhóm endpoint khác từ lát 2B-1b trở đi đều đi qua
`dataset_context` + `tenant_context`.

Handler viết bằng `def` chứ không `async def`: chúng gọi SQLAlchemy đồng bộ, và
FastAPI chạy handler đồng bộ trong threadpool. Viết `async def` ở đây sẽ chặn
vòng lặp sự kiện suốt thời gian băm Argon2id (~100 ms) — tức là cả server đứng
lại mỗi lần có người đăng nhập.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request, status

from ket.api.dependencies import (
    AnyStatePrincipal,
    AppSettings,
    EnrollingPrincipal,
    PasswordChangePrincipal,
    SecretBoxes,
    SessionFactory,
)
from ket.api.middleware.request_context import client_info_of, correlation_id_of
from ket.api.routers.auth_schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    TotpConfirmRequest,
    TotpEnrollRequest,
    TotpEnrollResponse,
)
from ket.kernel.persistence.session import control_session
from ket.kernel.security import account_service, auth_service
from ket.kernel.security.auth_models import SessionScope

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    factory: SessionFactory,
    settings: AppSettings,
    secret_boxes: SecretBoxes,
) -> LoginResponse:
    """Cấp phiên đăng nhập (FR-NFR-010, FR-NFR-016).

    Không mở transaction ở đây: `authenticate` phải tự quản transaction của nó
    vì đường **thất bại** cũng cần commit (bộ đếm khóa + nhật ký). Xem
    `kernel/security/auth_service.py`.
    """
    issued = auth_service.authenticate(
        factory,
        username=payload.username,
        password=payload.password.get_secret_value(),
        totp_code=payload.totp_code,
        secret_box_provider=secret_boxes,
        session_ttl=timedelta(minutes=settings.session_ttl_minutes),
        client_info=client_info_of(request),
        correlation_id=correlation_id_of(request),
    )
    return LoginResponse(
        token=issued.token,
        expires_at=issued.expires_at,
        must_change_password=issued.must_change_password,
        session_scope=issued.scope,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    principal: AnyStatePrincipal,
    factory: SessionFactory,
) -> None:
    """Thu hồi phiên đang dùng. Gọi lại với token đã thu hồi trả 401.

    Làm được ở **mọi** trạng thái phiên: một người đang kẹt ở màn hình đăng ký
    2FA hay màn hình đổi mật khẩu tạm vẫn phải đăng xuất được.
    """
    with control_session(factory) as session:
        auth_service.revoke_session(
            session,
            session_id=principal.session_id,
            expected_user_id=principal.user_id,
            actor_user_id=principal.user_id,
            correlation_id=correlation_id_of(request),
            client_info=client_info_of(request),
        )


@router.get("/me", response_model=MeResponse)
def me(principal: AnyStatePrincipal) -> MeResponse:
    """Danh tính của phiên hiện tại — client gọi lúc khởi động để khôi phục phiên.

    `session_scope` và `must_change_password` cho client biết phải đưa người
    dùng tới màn hình nào trước khi cho vào phần nghiệp vụ.
    """
    return MeResponse(
        user_id=principal.user_id,
        username=principal.username,
        locale=principal.locale,
        must_change_password=principal.must_change_password,
        expires_at=principal.expires_at,
        session_scope=principal.scope,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: PasswordChangePrincipal,
    factory: SessionFactory,
) -> None:
    """Chủ tài khoản tự đổi mật khẩu; mọi phiên **khác** bị thu hồi.

    Hai transaction: kiểm mật khẩu hiện tại (có bộ đếm khóa + nhật ký, commit cả
    khi sai — xem `auth_service.verify_current_password`), rồi mới ghi.

    Thu hồi phiên khác là phần không được quên: người ta đổi mật khẩu chính vì
    nghi nó đã lộ, và một phiên mở sẵn trên máy khác vẫn sống nếu không đuổi.
    """
    auth_service.verify_current_password(
        factory,
        username=principal.username,
        password=payload.current_password.get_secret_value(),
        correlation_id=correlation_id_of(request),
        client_info=client_info_of(request),
    )
    with control_session(factory) as session:
        user = account_service.find_user(session, principal.username)
        account_service.set_own_password(
            session,
            user=user,
            new_password=payload.new_password.get_secret_value(),
            correlation_id=correlation_id_of(request),
            client_info=client_info_of(request),
        )
        auth_service.revoke_other_sessions(
            session,
            user_id=user.id,
            keep_session_id=principal.session_id,
            actor_user_id=user.id,
            correlation_id=correlation_id_of(request),
            client_info=client_info_of(request),
        )


@router.post("/totp/enroll", response_model=TotpEnrollResponse)
def enroll_totp(
    payload: TotpEnrollRequest,
    request: Request,
    principal: EnrollingPrincipal,
    factory: SessionFactory,
    secret_boxes: SecretBoxes,
) -> TotpEnrollResponse:
    """Sinh bí mật 2FA mới và trả URI dựng mã QR. Chưa có hiệu lực tới khi xác nhận.

    Gọi được bằng **phiên hạn chế** — đó là toàn bộ lý do phiên hạn chế tồn tại:
    tài khoản bị bắt bật 2FA mà chưa có thiết bị thì đây là endpoint duy nhất nó
    còn dùng được.
    """
    auth_service.verify_current_password(
        factory,
        username=principal.username,
        password=payload.password.get_secret_value(),
        correlation_id=correlation_id_of(request),
        client_info=client_info_of(request),
    )
    with control_session(factory) as session:
        user = account_service.find_user(session, principal.username)
        uri = account_service.begin_totp_enrollment(session, user=user, secret_box=secret_boxes())
    return TotpEnrollResponse(provisioning_uri=uri)


@router.post("/totp/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_totp(
    payload: TotpConfirmRequest,
    request: Request,
    principal: EnrollingPrincipal,
    factory: SessionFactory,
    secret_boxes: SecretBoxes,
) -> None:
    """Xác nhận thiết bị sinh mã hoạt động — từ đây 2FA mới chặn được đăng nhập.

    Thu hồi luôn phiên đang dùng khi nó là phiên hạn chế: phiên đó được cấp cho
    một tài khoản **chưa** qua lớp thứ hai, nên nó không được tự nâng cấp thành
    phiên đầy đủ. Người dùng đăng nhập lại và lần này nhập mã — đó cũng là lần
    thử thật đầu tiên của thiết bị vừa đăng ký.
    """
    with control_session(factory) as session:
        user = account_service.find_user(session, principal.username)
        account_service.confirm_totp_enrollment(
            session,
            user=user,
            code=payload.code,
            secret_box=secret_boxes(),
            correlation_id=correlation_id_of(request),
            client_info=client_info_of(request),
        )
        if principal.scope is not SessionScope.FULL:
            auth_service.revoke_session(
                session,
                session_id=principal.session_id,
                expected_user_id=principal.user_id,
                actor_user_id=principal.user_id,
                correlation_id=correlation_id_of(request),
                client_info=client_info_of(request),
            )
