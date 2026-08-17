"""Gán vai trò, quyền và chi nhánh cho người dùng trong một dữ liệu kế toán.

Hai nhóm hàm, hai bối cảnh chạy khác hẳn nhau — đừng trộn:

* **Gieo mầm** (`sync_permissions`, `ensure_admin_role`) chạy bằng `ket_owner`
  trên một `Connection` trần, lúc tạo dữ liệu kế toán và ở đường sửa chữa. Không
  đi qua ORM `Session` nên **không** sinh dòng `audit_log` — có chủ đích: lúc đó
  chưa có người dùng nào để ghi vào `audit_log.user_id`, và bản thân việc gieo
  mầm là một phần của migration chứ không phải một thao tác nghiệp vụ.
* **Gán quyền** (`grant_role`, `revoke_role`, `assign_branch`, `revoke_branch`)
  chạy bằng vai trò runtime trong transaction đã định tuyến, qua ORM, nên mọi
  thay đổi đều có vết (FR-NFR-012).

## Thứ tự fail-safe khi vai trò kéo theo 2FA

`users.totp_required` là cờ **toàn cục** còn vai trò là **per-dataset** (quyết
định B của lát 2B-1a: đăng nhập chạy trước khi chọn dataset, nên lúc kiểm mật
khẩu hệ thống chưa biết người này sắp mở doanh nghiệp nào). Vì vậy một lần gán
vai trò nhạy cảm phải chạm hai schema, và hai schema nghĩa là hai transaction —
không có cách nào làm nó nguyên tử.

Thứ tự được chọn để **hướng hỏng** là an toàn: bật cờ trước, ghi vai trò sau.

* Hỏng giữa chừng theo thứ tự này → người dùng bị đòi 2FA mà chưa có quyền gì
  thêm. Phiền, tự sửa được, và lát này đã có phiên hạn chế để họ tự đăng ký
  thiết bị.
* Thứ tự ngược lại → người dùng có quyền quản trị **mà không** bị đòi lớp thứ
  hai, và không có gì báo. Đó là FR-NFR-016 bị vô hiệu trong im lặng.

Đây cũng là lý do vai trò dataset (`ds_<mã>_app`) không cần — và không được —
quyền ghi trên `public.users`: cờ do transaction điều khiển đặt, chạy dưới
`ket_app`, trước khi phiên chuyển sang vai trò dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.control_log import ControlAuditAction, record_control_action
from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.models import User
from ket.kernel.errors import (
    BranchNotFoundError,
    BranchNotInScopeError,
    RoleNotFoundError,
    UserNotFoundError,
)
from ket.kernel.persistence.seeding import bind_seed_schema
from ket.kernel.persistence.session import control_session, dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import (
    Branch,
    Permission,
    Role,
    RolePermission,
    UserBranch,
    UserRole,
)
from ket.kernel.security.permissions import REGISTRY, PermissionRegistry

ADMIN_ROLE_CODE = "admin"
"""Vai trò hệ thống duy nhất được gieo sẵn.

Một chứ không phải ba (`admin`/`ke-toan`/`xem`): bộ vai trò mẫu thật thuộc gói
cấu hình TT200/TT133 ở phase 5, và gieo sẵn một bộ nửa vời bây giờ sẽ tạo ra dữ
liệu mà phase 5 phải đi dọn. Cái duy nhất bắt buộc phải có ngay là **một đường
để người đầu tiên làm được việc** — nếu không, dữ liệu kế toán vừa tạo là một
cái hộp không ai mở được."""

ADMIN_ROLE_NAME = "Quản trị hệ thống"


@dataclass(frozen=True)
class SeedResult:
    """Số lượng bản ghi **thêm mới** của một lần gieo mầm. Đã đúng thì toàn 0."""

    permissions_added: int
    role_permissions_added: int
    role_created: bool


def _bind_schema(connection: Connection, schema: str) -> None:
    """Chuẩn bị connection cho một hàm gieo mầm — xem `persistence/seeding.py`.

    Giữ lại tên riêng ở đây thay vì gọi thẳng khắp tệp: mọi hàm gieo của module
    này gọi nó ở dòng đầu, và một cái tên ngắn tại chỗ đọc dễ hơn một đường dẫn
    import dài lặp lại năm lần.
    """
    bind_seed_schema(connection, schema)


def sync_permissions(
    connection: Connection, schema: str, *, registry: PermissionRegistry = REGISTRY
) -> int:
    """Đưa mọi mã quyền của registry vào bảng `permissions`. Trả số mã thêm mới.

    Thêm-nếu-thiếu chứ không xóa-rồi-ghi-lại: `role_permissions` trỏ tới
    `permissions.id`, nên ghi lại từ đầu sẽ xóa sạch phân quyền của khách hàng
    mỗi lần nâng cấp.

    Mã **biến mất khỏi registry** (module bị bỏ) cố ý được giữ lại trong bảng:
    xóa nó sẽ kéo theo `role_permissions` và làm mất thông tin "vai trò này từng
    được cấp quyền gì" — thứ kiểm toán viên hỏi. Dọn là thao tác riêng, có người
    quyết định.
    """
    _bind_schema(connection, schema)
    existing = set(connection.scalars(select(Permission.code)).all())
    missing = [code for code in registry.codes() if code not in existing]
    if missing:
        connection.execute(insert(Permission), [{"code": code} for code in missing])
    return len(missing)


def ensure_admin_role(
    connection: Connection, schema: str, *, registry: PermissionRegistry = REGISTRY
) -> SeedResult:
    """Gieo bảng quyền + vai trò `admin` đầy đủ quyền. Chạy lại được.

    Chạy lại được là yêu cầu thật, không phải phòng xa: mỗi lần thêm loại chứng
    từ mới (phase 6 trở đi), `ensure-cluster` phải cấp mã quyền mới cho `admin`
    của **mọi** dữ liệu kế toán đã tồn tại — nếu không, quản trị viên của một
    doanh nghiệp cũ sẽ không mở được màn hình vừa thêm.
    """
    permissions_added = sync_permissions(connection, schema, registry=registry)

    role_id = connection.scalar(select(Role.id).where(Role.code == ADMIN_ROLE_CODE))
    created = role_id is None
    if role_id is None:
        role_id = connection.scalar(
            insert(Role)
            .values(code=ADMIN_ROLE_CODE, name=ADMIN_ROLE_NAME, name_en="System administrator")
            .returning(Role.id)
        )

    granted = set(
        connection.scalars(
            select(RolePermission.permission_id).where(RolePermission.role_id == role_id)
        ).all()
    )
    to_grant = [
        permission_id
        for permission_id in connection.scalars(select(Permission.id)).all()
        if permission_id not in granted
    ]
    if to_grant:
        connection.execute(
            insert(RolePermission),
            [
                {"role_id": role_id, "permission_id": permission_id, "allow": True}
                for permission_id in to_grant
            ],
        )

    return SeedResult(
        permissions_added=permissions_added,
        role_permissions_added=len(to_grant),
        role_created=created,
    )


def seed_registered_datasets(owner_engine: Engine, schemas: tuple[str, ...]) -> None:
    """Gieo mầm cho một loạt schema dataset trong **một** transaction đặc quyền."""
    with owner_engine.begin() as connection:
        for schema in schemas:
            ensure_admin_role(connection, schema)
        # `search_path` là thuộc tính của session DB, và connection này quay lại
        # pool sau khi thoát. `SET LOCAL` đã giới hạn theo transaction, nhưng nêu
        # lại ở đây thì người đọc không phải tự suy ra điều đó.
        connection.exec_driver_sql("SET LOCAL search_path TO public")


def _role_requires_second_factor(
    session: Session, role_id: int, *, registry: PermissionRegistry = REGISTRY
) -> bool:
    """Vai trò này có mang quyền thuộc diện bắt buộc 2FA không (FR-SYS-074)?

    Tính từ **quyền thật đang gán**, không từ mã vai trò: một vai trò tên
    "kế toán viên" được cấp thêm `system.role.edit` vẫn là vai trò quản trị, và
    một danh sách mã vai trò nhạy cảm viết cứng sẽ không thấy điều đó.

    **Ràng buộc cho lát sau:** hàm này chỉ chạy lúc **gán vai trò**. Mọi đường
    ghi `role_permissions` (màn hình sửa vai trò, gói cấu hình TT200/TT133 ở
    phase 5) **phải** chạy lại bước bật cờ cho những `user_roles` đang trỏ tới
    vai trò đó — thêm một quyền nhạy cảm vào vai trò đã có người giữ mà không
    làm việc đó là vô hiệu FR-NFR-016 trong im lặng. Hôm nay chưa hở: nguồn ghi
    `role_permissions` duy nhất là `ensure_admin_role`, và nó cấp **toàn bộ**
    quyền cho `admin` ngay từ đầu.
    """
    sensitive = registry.second_factor_codes()
    if not sensitive:  # pragma: no cover - registry luôn có ít nhất một mã
        return False
    codes = session.scalars(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id, RolePermission.allow.is_(True))
    ).all()
    return any(code in sensitive for code in codes)


def _prepare_target_user(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    require_second_factor: bool,
    actor_user_id: int,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> bool:
    """Kiểm người nhận có thật, và bật `totp_required` nếu vai trò đòi.

    Trả `True` nếu cờ vừa đổi. Transaction riêng trên schema điều khiển, chạy
    **trước** khi vai trò được ghi — xem phần "thứ tự fail-safe" ở đầu tệp.

    Kiểm sự tồn tại chạy cho **mọi** lần gán, không riêng vai trò nhạy cảm.
    `user_roles.user_id` không có khóa ngoại (bảng nằm ở schema dataset còn
    `users` ở schema điều khiển, và ràng buộc chéo schema sẽ làm bản dump của
    một dataset không restore được sang cụm khác), nên **không có gì** ở tầng DB
    thay ta kiểm. Hại thật không phải một id bịa mà là một id **gõ nhầm nhưng có
    thật**: cấp vai trò cho nhầm người, im lặng, và người quản trị tin rằng mình
    đã cấp cho đúng người.

    `with_for_update()` cùng khuôn với `authenticate`: hai lần gán song song cho
    cùng một người sẽ đọc–sửa–ghi trên cùng dòng `users`, và không khóa thì nhật
    ký ghi hai dòng "totp_required: False → True" cho một lần đổi thật.
    """
    with control_session(factory) as session:
        user = session.scalars(
            select(User).where(User.id == user_id).with_for_update()
        ).one_or_none()
        if user is None:
            raise UserNotFoundError("Không có người dùng với mã này", user_id=user_id)
        if not require_second_factor or user.totp_required:
            return False
        user.totp_required = True
        record_control_action(
            session,
            action=ControlAuditAction.USER_UPDATED,
            actor_user_id=actor_user_id,
            subject_user_id=user.id,
            subject_label=user.username,
            old_values={"totp_required": False},
            new_values={"totp_required": True},
            correlation_id=correlation_id,
            client_info=client_info,
        )
        return True


def _scope_for(
    dataset_schema: str, actor_user_id: int, correlation_id: UUID | None
) -> RequestScope:
    """Ngữ cảnh ghi cho thao tác phân quyền.

    `branch_ids=()` có chủ đích: bốn bảng phân quyền không bật RLS (chúng là
    **nguồn** dựng nên phạm vi RLS), và dòng `audit_log` của thao tác này mang
    `branch_id = NULL` — sự kiện mức dữ liệu kế toán, không thuộc chi nhánh nào.
    Policy nhật ký cho `NULL` đi qua đúng vì lý do đó.
    """
    return RequestScope(
        dataset_schema=dataset_schema,
        user_id=actor_user_id,
        branch_ids=(),
        correlation_id=correlation_id,
    )


def _find_role(session: Session, role_code: str) -> Role:
    role = session.scalars(select(Role).where(Role.code == role_code)).one_or_none()
    if role is None:
        raise RoleNotFoundError("Không có vai trò với mã này", role=role_code)
    return role


def grant_role(
    factory: sessionmaker[Session],
    *,
    dataset_schema: str,
    user_id: int,
    role_code: str,
    actor_user_id: int,
    correlation_id: UUID | None = None,
    client_info: str | None = None,
) -> bool:
    """Gán vai trò cho người dùng trong một dữ liệu kế toán. Trả `True` nếu vừa gán.

    Ba transaction, theo đúng thứ tự này (xem phần fail-safe ở đầu tệp):
    đọc vai trò → kiểm người nhận + bật cờ 2FA nếu cần → ghi `user_roles`.
    """
    with dataset_session(
        factory,
        dataset_schema=dataset_schema,
        branch_ids=(),
        audit=_scope_for(dataset_schema, actor_user_id, correlation_id).audit_context(),
    ) as session:
        role = _find_role(session, role_code)
        role_id = role.id
        needs_second_factor = _role_requires_second_factor(session, role_id)

    _prepare_target_user(
        factory,
        user_id=user_id,
        require_second_factor=needs_second_factor,
        actor_user_id=actor_user_id,
        correlation_id=correlation_id,
        client_info=client_info,
    )

    with unit_of_work(
        factory, _scope_for(dataset_schema, actor_user_id, correlation_id)
    ) as session:
        # Khóa dòng `roles` trước khi đọc–sửa–ghi trên `user_roles`. Không khóa
        # thì hai lần gán song song cùng đọc "chưa có" rồi cùng `INSERT`, và lần
        # thua cuộc nhận `UniqueViolation` — tức là **HTTP 500** cho đúng thao
        # tác mà `GrantResponse` vừa hứa là gọi lại được. Đo được: 4 luồng cùng
        # gán một cặp (người dùng, vai trò) → 2 lỗi.
        #
        # Khóa `roles` chứ không `ON CONFLICT DO NOTHING`: câu upsert là SQL
        # Core, không đi qua listener nhật ký, nên nó sẽ ghi quyền mà **không để
        # lại vết** (FR-NFR-012). Giá phải trả là các lần gán **cùng một vai
        # trò** nối đuôi nhau — thao tác quản trị, thưa, không nằm trên đường
        # nóng nào.
        session.get(Role, role_id, with_for_update=True)
        existing = session.get(UserRole, {"user_id": user_id, "role_id": role_id})
        if existing is not None:
            return False
        session.add(UserRole(user_id=user_id, role_id=role_id))
    return True


def revoke_role(
    factory: sessionmaker[Session],
    *,
    dataset_schema: str,
    user_id: int,
    role_code: str,
    actor_user_id: int,
    correlation_id: UUID | None = None,
) -> bool:
    """Gỡ vai trò. Trả `True` nếu vừa gỡ.

    **Không** tắt `totp_required`. Cờ là toàn cục còn vai trò là per-dataset, nên
    "người này còn cần 2FA không" chỉ trả lời được sau khi quét mọi dữ liệu kế
    toán — và trả lời sai theo chiều tắt đi là gỡ mất lớp bảo vệ. Tắt cờ là thao
    tác tường minh của quản trị viên (`ket.admin reset-totp`).
    """
    scope = _scope_for(dataset_schema, actor_user_id, correlation_id)
    with unit_of_work(factory, scope) as session:
        role = _find_role(session, role_code)
        session.get(Role, role.id, with_for_update=True)
        existing = session.get(UserRole, {"user_id": user_id, "role_id": role.id})
        if existing is None:
            return False
        session.delete(existing)
    return True


def _resolve_branch_id(
    factory: sessionmaker[Session], *, dataset_schema: str, branch_code: str, actor_user_id: int
) -> int:
    """Mã chi nhánh → id, trong một transaction đọc riêng.

    Phải đọc **trước** transaction ghi, không phải trong nó: dòng `user_branches`
    mang `branch_id`, và listener nhật ký lấy chi nhánh của **bản ghi** làm chi
    nhánh của dòng nhật ký. `WITH CHECK` của policy vì thế đòi chính chi nhánh đó
    phải nằm trong GUC phạm vi — mà GUC được đặt lúc mở transaction, tức là trước
    khi biết id. Đọc trước là cách duy nhất thoát vòng lặp đó mà không nới policy.

    `branches` cố ý không bật RLS (xem migration `0001`) nên lượt đọc này chạy
    được với phạm vi rỗng.
    """
    with dataset_session(
        factory,
        dataset_schema=dataset_schema,
        branch_ids=(),
        audit=AuditContext(user_id=actor_user_id),
    ) as session:
        branch_id = session.scalar(select(Branch.id).where(Branch.code == branch_code))
    if branch_id is None:
        raise BranchNotFoundError("Không có chi nhánh với mã này", branch=branch_code)
    return branch_id


def _branch_scope(
    dataset_schema: str,
    actor_user_id: int,
    branch_id: int,
    actor_branch_ids: tuple[int, ...] | None,
    correlation_id: UUID | None,
) -> RequestScope:
    """Ngữ cảnh ghi cho thao tác gán/thu chi nhánh.

    Phạm vi là của **người thực hiện** khi có (đường HTTP), và đúng chi nhánh
    đích khi không (đường CLI phá-kính, lúc chưa ai có chi nhánh nào). Chi nhánh
    đích luôn nằm trong phạm vi vì `_check_actor_may_touch_branch` đã chặn trước
    — điều kiện để dòng `audit_log` (mang `branch_id` của bản ghi) qua được
    `WITH CHECK` của policy.
    """
    scope = actor_branch_ids if actor_branch_ids is not None else (branch_id,)
    return RequestScope(
        dataset_schema=dataset_schema,
        user_id=actor_user_id,
        branch_ids=tuple(sorted(set(scope))),
        correlation_id=correlation_id,
        acting_branch_id=branch_id,
    )


def _check_actor_may_touch_branch(
    branch_id: int, branch_code: str, actor_branch_ids: tuple[int, ...] | None
) -> None:
    """Người thực hiện chỉ gán/thu được chi nhánh **họ đang thấy**.

    Không có luật này thì cô lập chi nhánh (FR-SYS-072, RT-04) không ngăn được
    ai nắm `system.user.edit`: họ tự gán cho chính mình mọi chi nhánh còn lại
    trong một request, và dòng nhật ký của thao tác đó mang `branch_id` của chi
    nhánh **đích** — tức là vô hình với chính người vừa bị vượt qua. Đo được
    trước khi sửa: phạm vi `[2]` → tự gán → `[2, 3]`, HTTP 200.

    Luật này **không** chặn công việc thật: kế toán trưởng toàn công ty giữ mọi
    chi nhánh nên vẫn gán được mọi chi nhánh. Nó chỉ chặn đúng người giữ một
    phần tự nới sang phần còn lại.

    `actor_branch_ids=None` = đường **phá-kính tại máy chủ** (`ket.admin
    grant-branch`): lúc dựng bản cài, chưa ai có chi nhánh nào, nên đòi hỏi trên
    sẽ khóa mọi người ra ngoài. Ai chạm được máy chủ thì đã chạm được DB — đây
    là thừa nhận thực tế, không phải một lỗ mới.
    """
    if actor_branch_ids is None:
        return
    if branch_id not in actor_branch_ids:
        raise BranchNotInScopeError(
            "Không gán được chi nhánh nằm ngoài phạm vi của người thực hiện",
            branch=branch_code,
        )


def assign_branch(
    factory: sessionmaker[Session],
    *,
    dataset_schema: str,
    user_id: int,
    branch_code: str,
    actor_user_id: int,
    actor_branch_ids: tuple[int, ...] | None,
    correlation_id: UUID | None = None,
) -> bool:
    """Cho người dùng thấy một chi nhánh (FR-SYS-072). Trả `True` nếu vừa gán.

    Không gán chi nhánh nào = **không thấy dòng nào** có `branch_id`, chứ không
    phải "thấy tất". Đó là trạng thái đúng của một tài khoản mới và là hướng
    hỏng đã chọn cho toàn bộ cơ chế RLS.

    `actor_branch_ids` bắt buộc khai (không có mặc định): xem
    `_check_actor_may_touch_branch`. Bỏ qua nó là mở lại đúng đường tự nới phạm
    vi, nên nó không được phép là thứ người gọi quên.
    """
    branch_id = _resolve_branch_id(
        factory,
        dataset_schema=dataset_schema,
        branch_code=branch_code,
        actor_user_id=actor_user_id,
    )
    _check_actor_may_touch_branch(branch_id, branch_code, actor_branch_ids)
    _prepare_target_user(
        factory,
        user_id=user_id,
        require_second_factor=False,
        actor_user_id=actor_user_id,
        correlation_id=correlation_id,
    )

    scope = _branch_scope(
        dataset_schema, actor_user_id, branch_id, actor_branch_ids, correlation_id
    )
    with unit_of_work(factory, scope) as session:
        # Khóa dòng `branches` — cùng lý do với `roles` ở `grant_role`.
        session.get(Branch, branch_id, with_for_update=True)
        existing = session.get(UserBranch, {"user_id": user_id, "branch_id": branch_id})
        if existing is not None:
            return False
        session.add(UserBranch(user_id=user_id, branch_id=branch_id))
    return True


def revoke_branch(
    factory: sessionmaker[Session],
    *,
    dataset_schema: str,
    user_id: int,
    branch_code: str,
    actor_user_id: int,
    actor_branch_ids: tuple[int, ...] | None,
    correlation_id: UUID | None = None,
) -> bool:
    """Thu lại quyền xem một chi nhánh. Trả `True` nếu vừa thu.

    Cùng luật phạm vi với `assign_branch`: không thu được chi nhánh mình không
    thấy. Chiều thu tưởng vô hại nhưng nó là công cụ khóa người khác ra khỏi
    chi nhánh của họ.
    """
    branch_id = _resolve_branch_id(
        factory,
        dataset_schema=dataset_schema,
        branch_code=branch_code,
        actor_user_id=actor_user_id,
    )
    _check_actor_may_touch_branch(branch_id, branch_code, actor_branch_ids)

    scope = _branch_scope(
        dataset_schema, actor_user_id, branch_id, actor_branch_ids, correlation_id
    )
    with unit_of_work(factory, scope) as session:
        session.get(Branch, branch_id, with_for_update=True)
        existing = session.get(UserBranch, {"user_id": user_id, "branch_id": branch_id})
        if existing is None:
            return False
        session.delete(existing)
    return True
