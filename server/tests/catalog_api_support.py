"""Khung dựng người dùng – vai trò – chi nhánh cho test danh mục qua HTTP.

Tách khỏi `test_master_data_api.py` khi lát 3B-2 thêm tệp test thứ hai đi qua
đúng chuỗi đó (đối tác, nhân viên, gộp bản ghi, tài khoản ngân hàng). Chép sang
tệp thứ hai là cách hai bản sao lệch nhau: bản này cấp quyền theo registry, bản
kia gõ tay một mã quyền, và tệp nào cũng xanh cho tới lúc một trong hai đo sai
điều nó tưởng đang đo.

Ở đây **chỉ** có phần dựng bối cảnh. Luật nghiệp vụ nằm trong từng tệp test, nơi
người đọc thấy được cái được khẳng định ngay cạnh cái được dựng.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.api.dependencies import DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch, Permission, Role, RolePermission
from ket.kernel.security.permissions import Action

UserFactory = Callable[..., User]


def unique_code(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8].upper()}"


def catalog_codes(slug: str, *actions: Action) -> list[str]:
    """Mã quyền của một danh mục, lấy từ registry chứ không gõ tay.

    Gõ tay thì một lần đổi `slug` sẽ để test xanh trong khi người dùng thật nhận
    `403` — test cấp cho mình một mã quyền không còn ai kiểm.
    """
    spec = REGISTRY.get(slug)
    assert spec is not None, f"test viết cho danh mục {slug} nhưng nó không còn đăng ký"
    return [spec.permission_code(action) for action in actions]


def create_record(
    client: TestClient,
    headers: dict[str, str],
    slug: str,
    body: dict[str, object],
    *,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/v1/master/{slug}",
        json=body,
        headers={**headers, IDEMPOTENCY_HEADER: key or uuid4().hex},
    )


def ensure_role(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    code: str,
    permissions: list[str],
) -> str:
    """Vai trò với đúng bộ mã quyền đã cho; gọi lại lần hai không tạo trùng.

    Vai trò đã tồn tại mà bộ quyền LỆCH thì ném ngay (review 6E-2, M-1): hai
    tệp test dùng chung một mã vai trò với hai bộ quyền khác nhau làm bộ test
    phụ thuộc thứ tự tệp — tệp chạy trước thắng, và tệp chạy sau khẳng định 403
    vì một lý do khác lý do nó viết ra. Im lặng bỏ qua là cách lỗi đó sống sót
    tới ngày ai đó đổi tên tệp.
    """
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        existing_id = session.scalar(select(Role.id).where(Role.code == code))
        if existing_id is not None:
            granted = set(
                session.scalars(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id == existing_id)
                ).all()
            )
            assert granted == set(permissions), (
                f"vai trò {code!r} đã tồn tại với bộ quyền khác: "
                f"thiếu {sorted(set(permissions) - granted)}, thừa {sorted(granted - set(permissions))}. "
                "Đổi mã vai trò của tệp test này — dùng chung mã làm bộ test phụ thuộc thứ tự."
            )
        else:
            role = Role(code=code, name=f"Vai trò {code}")
            session.add(role)
            session.flush()
            for permission in permissions:
                permission_id = session.scalar(
                    select(Permission.id).where(Permission.code == permission)
                )
                assert permission_id is not None, (
                    f"chưa gieo mã quyền {permission} — registry danh mục chưa được nạp "
                    "lúc cấp dữ liệu kế toán"
                )
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return code


def ensure_branches(
    session_factory: sessionmaker[Session], dataset: DatasetRef, codes: list[str]
) -> list[str]:
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        existing = set(session.scalars(select(Branch.code).where(Branch.code.in_(codes))).all())
        for code in codes:
            if code not in existing:
                BranchService(session).create(code=code, name=f"Chi nhánh {code}")
    return codes


def all_branch_codes(session_factory: sessionmaker[Session], dataset: DatasetRef) -> list[str]:
    """Mọi chi nhánh đang có trong dữ liệu kế toán.

    Dùng để dựng người có **phạm vi toàn công ty** — điều kiện của thao tác gộp
    bản ghi (`merge_records`). Đọc lúc chạy chứ không viết cứng một danh sách:
    các tệp test khác cũng tạo chi nhánh trong cùng dataset dùng chung, nên một
    danh sách cố định sẽ đúng hôm nay và thiếu vào ngày có tệp test thứ mười.
    """
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        return list(session.scalars(select(Branch.code).order_by(Branch.code)).all())


def branch_ids(
    session_factory: sessionmaker[Session], dataset: DatasetRef, codes: list[str]
) -> dict[str, int]:
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        return {
            code: session.scalar(select(Branch.id).where(Branch.code == code)) or 0
            for code in codes
        }


def actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role_code: str,
    prefix: str,
    test_password: str,
    *,
    branch_codes: list[str],
) -> dict[str, str]:
    """Người dùng đã có vai trò, đã gán chi nhánh, đã đăng nhập — trả về header."""
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    for branch_code in branch_codes:
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset.schema_name,
            user_id=user.id,
            branch_code=branch_code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": test_password}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['token']}",
        DATASET_HEADER: dataset.code,
    }
