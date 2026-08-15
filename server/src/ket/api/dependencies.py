"""Dependency dùng chung cho router.

Vì sao `get_current_principal` nằm ở đây chứ không ở `kernel/security`: nó biết
`Request`, biết header `Authorization`, biết cách FastAPI báo lỗi. `kernel` phải
gọi được từ worker và từ `ket.admin` — hai chỗ không có request nào — nên nó
không được kéo theo FastAPI. Bản thân luật xác thực vẫn nằm trọn trong
`kernel/security/auth_service.py`; tệp này chỉ nối dây.

**Lệch có chủ đích so với plan.** Plan gọi `dataset_context`/`tenant_context` là
*middleware*. Thi công bằng **dependency** vì `SET LOCAL ROLE`, `SET LOCAL
search_path` và GUC chi nhánh chỉ có tác dụng **bên trong** transaction nghiệp
vụ, mà middleware của Starlette chạy ngoài transaction đó. Dependency dựng
`RequestScope`; `unit_of_work` mở transaction và bind cả ba. Cùng ba lệnh, cùng
thứ tự, chỉ khác nơi treo.

**Mọi hàm ở đây là `def` chứ không `async def`.** Chúng gọi SQLAlchemy đồng bộ;
đặt trong coroutine sẽ chặn vòng lặp sự kiện và làm cả server đứng trong lúc chờ
DB. FastAPI chạy dependency đồng bộ trong threadpool — đó là hành vi muốn có.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ket.api.middleware.request_context import client_info_of, correlation_id_of
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.datasets.service import resolve_dataset
from ket.kernel.errors import (
    BranchNotInScopeError,
    DatasetHeaderMissingError,
    NotAuthenticatedError,
    PasswordChangeRequiredError,
    SessionScopeLimitedError,
)
from ket.kernel.persistence.session import control_session, dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope
from ket.kernel.security.auth_models import SessionScope
from ket.kernel.security.auth_service import Principal, SecretBoxProvider, resolve_session
from ket.kernel.security.authorization import Access, resolve_access
from ket.kernel.security.keystore import SecretBox, load_app_key
from ket.settings import Settings

BEARER_PREFIX = "Bearer "

DATASET_HEADER = "X-Dataset"
"""Mã dữ liệu kế toán mà request đang mở (quyết định E1).

Header mỗi request chứ không gắn vào phiên: danh tính là toàn cục còn quyền là
per-dataset, nên một người mở hai doanh nghiệp ở hai cửa sổ là việc bình thường
— và cách này không cần cấp hai phiên để làm được điều đó.
"""

BRANCH_HEADER = "X-Branch"
"""Chi nhánh **đang thao tác**, khi người dùng được gán nhiều chi nhánh.

Khác `X-Dataset` ở chỗ nó không mở rộng tầm nhìn: phạm vi RLS luôn là toàn bộ
chi nhánh được gán, còn header này chỉ nói bản ghi mới thuộc chi nhánh nào và
dòng nhật ký mang chi nhánh nào. Ngoài phạm vi được gán → 403.
"""


def get_settings(request: Request) -> Settings:
    """Cấu hình của tiến trình, gắn vào app lúc `create_app`."""
    settings = request.app.state.settings
    if not isinstance(settings, Settings):  # pragma: no cover - bất biến nội bộ
        # `raise` chứ không `assert`: `python -O` gỡ bỏ `assert`, và bản đóng gói
        # PyInstaller (S4) có thể chạy với cờ đó. Khi ấy hàm sẽ trả về bất kỳ thứ
        # gì có trong `app.state` thay vì hỏng rõ ràng.
        raise RuntimeError("app.state.settings chưa được đặt — dùng ket.main.create_app")
    return settings


def get_engine(request: Request) -> Engine:
    """Engine runtime (`ket_app`), dựng trong `lifespan`."""
    engine = request.app.state.engine
    if not isinstance(engine, Engine):  # pragma: no cover - bất biến nội bộ
        raise RuntimeError("app.state.engine chưa được đặt — app chưa chạy qua lifespan")
    return engine


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Nhà máy `Session` đã gắn listener nhật ký (dựng trong `lifespan`)."""
    factory = request.app.state.session_factory
    if not isinstance(factory, sessionmaker):  # pragma: no cover - bất biến nội bộ
        raise RuntimeError("app.state.session_factory chưa được đặt — app chưa chạy qua lifespan")
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


def _resolve_principal(request: Request, factory: sessionmaker[Session]) -> Principal:
    """Người dùng đứng sau request. Token sai/hết hạn → 401.

    Mở một transaction riêng cho việc tra phiên, tách khỏi transaction nghiệp vụ
    của endpoint: hai việc có vòng đời khác nhau, và một lệnh ghi nghiệp vụ bị
    rollback không được kéo theo `last_seen_at` — cũng như ngược lại.
    """
    token = _bearer_token(request)
    with control_session(factory) as session:
        return resolve_session(session, token)


def principal_dependency(
    *, allow_limited_scope: bool = False, allow_password_change_pending: bool = False
) -> Callable[[Request, sessionmaker[Session]], Principal]:
    """Dựng dependency danh tính với đúng hai cổng được mở tường minh.

    Mặc định **đóng cả hai**: một endpoint mới không khai gì sẽ từ chối phiên
    hạn chế và từ chối tài khoản đang mang mật khẩu tạm. Hướng mặc định phải là
    chặn — người thêm endpoint sẽ quên nghĩ tới hai trạng thái này, và cái giá
    của việc quên theo chiều ngược lại là một tài khoản chưa có 2FA đi thẳng vào
    màn hình nghiệp vụ.
    """

    def dependency(
        request: Request,
        factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    ) -> Principal:
        principal = _resolve_principal(request, factory)
        if not allow_limited_scope and principal.scope is not SessionScope.FULL:
            raise SessionScopeLimitedError(
                "Phiên này chỉ dùng được để đăng ký thiết bị xác thực hai lớp",
                scope=principal.scope.value,
            )
        if not allow_password_change_pending and principal.must_change_password:
            raise PasswordChangeRequiredError("Phải đổi mật khẩu tạm trước khi tiếp tục")
        return principal

    return dependency


get_current_principal = principal_dependency()
"""Danh tính cho endpoint nghiệp vụ: phiên đầy đủ, mật khẩu đã đổi."""

get_principal_any_state = principal_dependency(
    allow_limited_scope=True, allow_password_change_pending=True
)
"""Cho `/auth/me` và `/auth/logout` — hai việc phải làm được ở **mọi** trạng
thái, kể cả khi đó là tất cả những gì tài khoản còn làm được."""

get_principal_enrolling = principal_dependency(
    allow_limited_scope=True, allow_password_change_pending=True
)
"""Cho đường đăng ký 2FA. Chính là lý do phiên hạn chế tồn tại."""

get_principal_changing_password = principal_dependency(allow_password_change_pending=True)
"""Cho `/auth/change-password`: mật khẩu tạm được, phiên hạn chế thì không —
người chưa qua được lớp thứ hai không được đổi mật khẩu của tài khoản."""


def get_dataset(
    request: Request,
    engine: Annotated[Engine, Depends(get_engine)],
) -> DatasetRef:
    """Dữ liệu kế toán mà request khai bằng header `X-Dataset`.

    Không có mặc định: đoán sai dataset là ghi sổ nhầm doanh nghiệp, và đó là
    loại sai không ai phát hiện cho tới kỳ quyết toán.
    """
    code = request.headers.get(DATASET_HEADER, "").strip()
    if not code:
        raise DatasetHeaderMissingError(
            f"Thiếu header {DATASET_HEADER} — request phải nói đang mở dữ liệu kế toán nào"
        )
    return resolve_dataset(engine, code)


def _acting_branch(request: Request, branch_ids: tuple[int, ...]) -> int | None:
    """Chi nhánh đang thao tác: header nếu có, suy ra nếu chỉ có một, còn lại `None`.

    Suy ra khi chỉ có một chi nhánh là tiện ích thật (phần lớn doanh nghiệp một
    chi nhánh sẽ không bao giờ gửi header này), nhưng **không** suy khi có nhiều
    — chọn hộ chi nhánh cho một bút toán là quyết định của kế toán viên.
    """
    raw = request.headers.get(BRANCH_HEADER, "").strip()
    if not raw:
        return branch_ids[0] if len(branch_ids) == 1 else None
    try:
        branch_id = int(raw)
    except ValueError:
        raise BranchNotInScopeError(f"Giá trị {BRANCH_HEADER} không hợp lệ", branch=raw) from None
    if branch_id not in branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )
    return branch_id


@dataclass(frozen=True)
class AuthorizedRequest:
    """Kết quả của một lần phân giải: ngữ cảnh ghi + quyền hiệu lực.

    Một đối tượng chứ hai dependency riêng, vì hai thứ này sinh ra từ **cùng
    một** lượt đọc DB: phạm vi chi nhánh vừa là trường của `RequestScope` vừa là
    trường của `Access`. Tách đôi sẽ hoặc đọc DB hai lần, hoặc phải chuyền kết
    quả qua `request.state` — một kênh không có kiểu.
    """

    dataset: DatasetRef
    scope: RequestScope
    access: Access


def get_authorized_request(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    dataset: Annotated[DatasetRef, Depends(get_dataset)],
    factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> AuthorizedRequest:
    """Phân giải dataset → vai trò → quyền → phạm vi chi nhánh cho một request.

    Chạy trong một transaction **đọc** riêng, trước transaction nghiệp vụ. Lý do
    là thứ tự: phạm vi chi nhánh (`user_branches`) là **đầu vào** của GUC RLS,
    nên nó phải biết trước khi transaction nghiệp vụ bind — mà `RequestScope`
    thì bất biến, dựng một lần với đủ dữ liệu.

    Giá phải trả là một lượt đọc thêm mỗi request. Chấp nhận được ở quy mô LAN
    (~20 người dùng đồng thời, ba câu truy vấn có index); nếu đo được là nghẽn
    thì chỗ sửa là bộ nhớ đệm theo (người dùng, dataset) với TTL ngắn, không
    phải nới lỏng thứ tự trên.
    """
    with dataset_session(
        factory,
        dataset_schema=dataset.schema_name,
        # Rỗng vì chính lượt đọc này là thứ tính ra phạm vi. Bốn bảng phân quyền
        # cố ý không bật RLS đúng để đường này chạy được — xem migration `0001`.
        branch_ids=(),
        audit=RequestScope(
            dataset_schema=dataset.schema_name, user_id=principal.user_id, branch_ids=()
        ).audit_context(),
    ) as session:
        access = resolve_access(session, user_id=principal.user_id)

    scope = RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=principal.user_id,
        branch_ids=access.branch_ids,
        correlation_id=correlation_id_of(request),
        client_info=client_info_of(request),
        acting_branch_id=_acting_branch(request, access.branch_ids),
    )
    return AuthorizedRequest(dataset=dataset, scope=scope, access=access)


def require_permission(code: str) -> Callable[[AuthorizedRequest], AuthorizedRequest]:
    """Dependency chặn khi thiếu một mã quyền (FR-NFR-011, FR-SYS-071).

    Trả về cả `AuthorizedRequest` để endpoint dùng tiếp (mở `unit_of_work`, kiểm
    thêm quyền khác) mà không phải khai thêm một dependency nữa.
    """

    def dependency(
        authorized: Annotated[AuthorizedRequest, Depends(get_authorized_request)],
    ) -> AuthorizedRequest:
        authorized.access.require(code)
        return authorized

    return dependency


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
AnyStatePrincipal = Annotated[Principal, Depends(get_principal_any_state)]
EnrollingPrincipal = Annotated[Principal, Depends(get_principal_enrolling)]
PasswordChangePrincipal = Annotated[Principal, Depends(get_principal_changing_password)]
SessionFactory = Annotated[sessionmaker[Session], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]
SecretBoxes = Annotated[SecretBoxProvider, Depends(get_secret_box_provider)]
AppEngine = Annotated[Engine, Depends(get_engine)]
Authorized = Annotated[AuthorizedRequest, Depends(get_authorized_request)]
