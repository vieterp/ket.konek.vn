"""Endpoint hệ thống (`/api/v1/system`) — bước 8 của phase 2.

Nhóm endpoint **đầu tiên** đi qua toàn bộ chuỗi định tuyến: header `X-Dataset` →
vai trò DB của dataset → `search_path` → phạm vi chi nhánh cho RLS → kiểm quyền.
Vì thế nó cũng là nơi chuỗi đó được chứng minh bằng test đầu-cuối, chứ không chỉ
bằng test tầng DB.

Bốn nhóm việc, mỗi nhóm một lý do tồn tại từ SRS:

* danh sách dữ liệu kế toán + quyền của tôi — client cần để dựng màn hình chọn
  và ẩn menu (FR-SYS-001);
* danh mục chi nhánh — neo của cô lập dữ liệu, phải tạo được trước mọi thứ khác
  (FR-SYS-072);
* gán vai trò / gán chi nhánh — FR-SYS-071/072, và là đường duy nhất qua HTTP để
  một người dùng mới làm được việc;
* đọc nhật ký — FR-NFR-013, đồng thời là bằng chứng RLS chặn đúng ở tầng API
  (bảng này có `branch_id` và bật RLS).

Handler viết `def` chứ không `async def` — xem lý do ở `routers/auth.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from ket.api.dependencies import (
    AppEngine,
    Authorized,
    AuthorizedRequest,
    SessionFactory,
    get_current_principal,
    require_permission,
)
from ket.api.routers.system_schemas import (
    AccessResponse,
    AuditEntryResponse,
    AuditListResponse,
    BranchCreateRequest,
    BranchGrantRequest,
    BranchListResponse,
    BranchResponse,
    DatasetListResponse,
    DatasetSummary,
    GrantResponse,
    RoleGrantRequest,
)
from ket.kernel.auditing.models import AuditLog
from ket.kernel.datasets.service import list_datasets
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code

router = APIRouter(prefix="/api/v1/system", tags=["system"])

BRANCH_VIEW = permission_code(SYSTEM_MODULE, "branch", Action.VIEW)
BRANCH_CREATE = permission_code(SYSTEM_MODULE, "branch", Action.CREATE)
ROLE_EDIT = permission_code(SYSTEM_MODULE, "role", Action.EDIT)
USER_EDIT = permission_code(SYSTEM_MODULE, "user", Action.EDIT)
AUDIT_VIEW = permission_code(SYSTEM_MODULE, "audit_log", Action.VIEW)

# Một alias cho mỗi quyền thay vì `Depends(...)` rải trong chữ ký hàm: mã quyền
# của một endpoint là thứ người review đọc trước tiên, nên nó nên nằm ở một chỗ
# đọc được thành danh sách.
BranchViewer = Annotated[AuthorizedRequest, Depends(require_permission(BRANCH_VIEW))]
BranchAuthor = Annotated[AuthorizedRequest, Depends(require_permission(BRANCH_CREATE))]
RoleAdmin = Annotated[AuthorizedRequest, Depends(require_permission(ROLE_EDIT))]
UserAdmin = Annotated[AuthorizedRequest, Depends(require_permission(USER_EDIT))]
AuditReader = Annotated[AuthorizedRequest, Depends(require_permission(AUDIT_VIEW))]

MAX_PAGE_SIZE = 200


def _branch_response(branch: Branch) -> BranchResponse:
    return BranchResponse(
        id=branch.id,
        code=branch.code,
        name=branch.name,
        name_en=branch.name_en,
        is_active=branch.is_active,
    )


@router.get(
    "/datasets",
    response_model=DatasetListResponse,
    dependencies=[Depends(get_current_principal)],
)
def list_available_datasets(engine: AppEngine) -> DatasetListResponse:
    """Dữ liệu kế toán đang hoạt động trên bản cài này.

    Không cần header `X-Dataset` — đây chính là endpoint client gọi để **biết**
    có thể đặt gì vào header đó.

    Trả về mọi dataset đang hoạt động chứ không lọc theo vai trò: lọc đòi mở
    từng schema để đọc `user_roles`, tức là N transaction cho một màn hình chọn.
    Không rò gì đáng kể — mã và tên doanh nghiệp trên bản cài mà người này đã
    đăng nhập được — còn cửa thật thì vẫn khóa: chọn một dataset không có vai trò
    sẽ nhận `dataset.access_denied` ở request kế tiếp.
    """
    return DatasetListResponse(
        items=[
            DatasetSummary(code=dataset.code, name=dataset.name, scheme=dataset.scheme)
            for dataset in list_datasets(engine)
        ]
    )


@router.get("/access", response_model=AccessResponse)
def my_access(authorized: Authorized) -> AccessResponse:
    """Quyền của tôi trong dữ liệu kế toán đang mở."""
    return AccessResponse(
        dataset_code=authorized.dataset.code,
        permissions=sorted(authorized.access.permissions),
        branch_ids=list(authorized.access.branch_ids),
        acting_branch_id=authorized.scope.acting_branch_id,
    )


@router.get("/branches", response_model=BranchListResponse)
def list_branches(authorized: BranchViewer, factory: SessionFactory) -> BranchListResponse:
    """Danh mục chi nhánh của dataset đang mở."""
    with unit_of_work(factory, authorized.scope) as session:
        branches = session.scalars(select(Branch).order_by(Branch.code)).all()
        return BranchListResponse(items=[_branch_response(branch) for branch in branches])


@router.post("/branches", response_model=BranchResponse, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreateRequest, authorized: BranchAuthor, factory: SessionFactory
) -> BranchResponse:
    """Tạo chi nhánh mới.

    Danh mục chi nhánh cố ý **không** bật RLS (xem migration `0001`): policy
    `WITH CHECK (id = ANY(scope))` sẽ khiến không ai tạo được chi nhánh đầu tiên,
    vì `id` do sequence cấp lúc `INSERT` và không thể nằm sẵn trong phạm vi của
    người tạo. Ai được sửa danh mục là câu hỏi của RBAC — chính dependency ở
    trên — không phải của cô lập dòng.
    """
    with unit_of_work(factory, authorized.scope) as session:
        branch = Branch(code=payload.code, name=payload.name, name_en=payload.name_en)
        session.add(branch)
        session.flush()
        return _branch_response(branch)


@router.post("/users/{user_id}/roles", response_model=GrantResponse)
def grant_role(
    user_id: int, payload: RoleGrantRequest, authorized: RoleAdmin, factory: SessionFactory
) -> GrantResponse:
    """Gán vai trò cho một người dùng trong dataset đang mở (FR-SYS-071).

    Vai trò mang quyền nhạy cảm sẽ bật `users.totp_required` **trước** khi ghi
    vai trò — xem thứ tự fail-safe ở `kernel/security/role_service.py`.
    """
    changed = role_service.grant_role(
        factory,
        dataset_schema=authorized.scope.dataset_schema,
        user_id=user_id,
        role_code=payload.role_code,
        actor_user_id=authorized.scope.user_id,
        correlation_id=authorized.scope.correlation_id,
        client_info=authorized.scope.client_info,
    )
    return GrantResponse(changed=changed)


@router.delete("/users/{user_id}/roles/{role_code}", response_model=GrantResponse)
def revoke_role(
    user_id: int, role_code: str, authorized: RoleAdmin, factory: SessionFactory
) -> GrantResponse:
    """Gỡ vai trò. Không tắt `totp_required` — xem `role_service.revoke_role`."""
    changed = role_service.revoke_role(
        factory,
        dataset_schema=authorized.scope.dataset_schema,
        user_id=user_id,
        role_code=role_code,
        actor_user_id=authorized.scope.user_id,
        correlation_id=authorized.scope.correlation_id,
    )
    return GrantResponse(changed=changed)


@router.post("/users/{user_id}/branches", response_model=GrantResponse)
def assign_branch(
    user_id: int, payload: BranchGrantRequest, authorized: UserAdmin, factory: SessionFactory
) -> GrantResponse:
    """Cho một người dùng thấy thêm một chi nhánh (FR-SYS-072).

    Chỉ gán được chi nhánh mà **chính người thực hiện** đang thấy: nếu không,
    ai có `system.user.edit` cũng tự nới phạm vi của mình sang mọi chi nhánh
    còn lại trong một request, và cô lập chi nhánh không ngăn được gì.
    """
    changed = role_service.assign_branch(
        factory,
        dataset_schema=authorized.scope.dataset_schema,
        user_id=user_id,
        branch_code=payload.branch_code,
        actor_user_id=authorized.scope.user_id,
        actor_branch_ids=authorized.access.branch_ids,
        correlation_id=authorized.scope.correlation_id,
    )
    return GrantResponse(changed=changed)


@router.delete("/users/{user_id}/branches/{branch_code}", response_model=GrantResponse)
def revoke_branch(
    user_id: int, branch_code: str, authorized: UserAdmin, factory: SessionFactory
) -> GrantResponse:
    """Thu lại quyền xem một chi nhánh. Cùng luật phạm vi với đường gán."""
    changed = role_service.revoke_branch(
        factory,
        dataset_schema=authorized.scope.dataset_schema,
        user_id=user_id,
        branch_code=branch_code,
        actor_user_id=authorized.scope.user_id,
        actor_branch_ids=authorized.access.branch_ids,
        correlation_id=authorized.scope.correlation_id,
    )
    return GrantResponse(changed=changed)


@router.get("/audit-log", response_model=AuditListResponse)
def read_audit_log(
    authorized: AuditReader,
    factory: SessionFactory,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> AuditListResponse:
    """Nhật ký nghiệp vụ của dataset đang mở (FR-NFR-013).

    Không có bộ lọc chi nhánh trong câu truy vấn, và đó là điểm chính: RLS trên
    bảng gốc cắt theo GUC `ket.branch_ids` mà transaction đã đặt, nên cả `total`
    lẫn danh sách đều chỉ thấy chi nhánh của người gọi. Một bộ lọc ở tầng ứng
    dụng quên ở đây sẽ không rò gì — đó là lý do cô lập nằm ở DB (RT-04).
    """
    with unit_of_work(factory, authorized.scope) as session:
        total = session.scalar(select(func.count()).select_from(AuditLog)) or 0
        rows = session.scalars(
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return AuditListResponse(
            total=total,
            items=[
                AuditEntryResponse(
                    id=row.id,
                    occurred_at=row.occurred_at,
                    user_id=row.user_id,
                    branch_id=row.branch_id,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    action=row.action,
                )
                for row in rows
            ],
        )
