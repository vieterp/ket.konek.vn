"""Dependency dùng chung cho router.

Vì sao `get_current_principal` nằm ở đây chứ không ở `kernel/security`: nó biết
`Request`, biết header `Authorization`, biết cách FastAPI báo lỗi. `kernel` phải
gọi được từ worker và từ `ket.admin` — hai chỗ không có request nào — nên nó
không được kéo theo FastAPI. Bản thân luật xác thực vẫn nằm trọn trong
`kernel/security/auth_service.py`; tệp này chỉ nối dây.

**Mọi hàm ở đây là `def` chứ không `async def`.** Chúng gọi SQLAlchemy đồng bộ;
đặt trong coroutine sẽ chặn vòng lặp sự kiện và làm cả server đứng trong lúc chờ
DB. FastAPI chạy dependency đồng bộ trong threadpool — đó là hành vi muốn có.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.errors import NotAuthenticatedError
from ket.kernel.persistence.session import control_session
from ket.kernel.security.auth_service import Principal, SecretBoxProvider, resolve_session
from ket.kernel.security.keystore import SecretBox, load_app_key
from ket.settings import Settings

BEARER_PREFIX = "Bearer "


def get_settings(request: Request) -> Settings:
    """Cấu hình của tiến trình, gắn vào app lúc `create_app`."""
    settings = request.app.state.settings
    assert isinstance(settings, Settings)  # noqa: S101 - bất biến nội bộ, không phải đầu vào
    return settings


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Nhà máy `Session` đã gắn listener nhật ký (dựng trong `lifespan`)."""
    factory = request.app.state.session_factory
    assert isinstance(factory, sessionmaker)  # noqa: S101 - bất biến nội bộ
    return factory


def get_secret_box_provider(request: Request) -> SecretBoxProvider:
    """Hàm nạp khóa app **khi cần**, có nhớ kết quả trong vòng đời tiến trình.

    Nạp trễ chứ không nạp lúc khởi động: một bản cài chưa cấu hình OS keystore
    vẫn phải khởi động và vẫn phải đăng nhập được cho tài khoản không bật 2FA
    (ADR-019). Chỉ thao tác thật sự chạm bí mật mới hỏng, và hỏng với thông điệp
    chỉ đúng cách sửa.
    """

    def provider() -> SecretBox:
        cached = getattr(request.app.state, "secret_box", None)
        if isinstance(cached, SecretBox):
            return cached
        settings = get_settings(request)
        override = settings.app_key.get_secret_value() if settings.app_key else None
        box = SecretBox(load_app_key(service=settings.keyring_service, override=override))
        request.app.state.secret_box = box
        return box

    return provider


def _bearer_token(request: Request) -> str:
    """Token từ header `Authorization: Bearer <token>`.

    Tự đọc header thay vì dùng `HTTPBearer` của FastAPI: `HTTPBearer` ném
    `HTTPException` với thân JSON riêng của nó, và như thế sẽ có **hai** định
    dạng lỗi trong cùng một API — RFC 7807 cho mọi thứ, trừ đúng chỗ này.
    """
    header = request.headers.get("authorization", "")
    if not header.startswith(BEARER_PREFIX):
        raise NotAuthenticatedError("Thiếu token phiên đăng nhập")
    token = header[len(BEARER_PREFIX) :].strip()
    if not token:
        raise NotAuthenticatedError("Thiếu token phiên đăng nhập")
    return token


def get_current_principal(
    request: Request,
    factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> Principal:
    """Người dùng đứng sau request. Token sai/hết hạn → 401.

    Mở một transaction riêng cho việc tra phiên, tách khỏi transaction nghiệp vụ
    của endpoint: hai việc có vòng đời khác nhau, và một lệnh ghi nghiệp vụ bị
    rollback không được kéo theo `last_seen_at` — cũng như ngược lại.
    """
    token = _bearer_token(request)
    with control_session(factory) as session:
        return resolve_session(session, token)


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SessionFactory = Annotated[sessionmaker[Session], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]
SecretBoxes = Annotated[SecretBoxProvider, Depends(get_secret_box_provider)]
