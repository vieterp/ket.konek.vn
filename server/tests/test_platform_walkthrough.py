"""Một hành trình duy nhất đi qua toàn bộ nền tảng phase 2 (bước 23).

Các tệp test khác kiểm từng cơ chế và kiểm rất kỹ. Tệp này kiểm thứ không cơ chế
nào tự kiểm được: **chúng có nối vào nhau không**, theo đúng thứ tự một người
dùng thật đi qua trong ngày đầu tiên —

    đăng nhập → bắt tay phiên bản → chọn dữ liệu kế toán → ghi một bản ghi có
    khóa idempotency → đính một tệp vào nó → xếp một tác vụ nền → đọc lại nhật
    ký → và cuối cùng: **vai trò runtime của chính app server không sửa nổi dòng
    nhật ký vừa sinh ra từ hành trình đó**.

Bước cuối là lý do tệp này tồn tại. `test_audit_immutability_owner_split` đã
chứng minh `ket_app` không sửa được `audit_log` — nhưng nó chứng minh trên một
dòng do chính test dựng bằng tay. Ở đây dòng ấy đến từ một request HTTP thật, đi
qua đúng vai trò DB, đúng `search_path`, đúng GUC chi nhánh mà bản cài thật
dùng. Một lát nào đó nối sai dây — ví dụ chạy transaction nghiệp vụ bằng vai trò
owner cho tiện — sẽ chỉ lộ ra ở đây.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.dependencies import DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.auditing.listener import AuditContext
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import dataset_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch, Permission, Role, RolePermission
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

WALKTHROUGH_ROLE = "ke_toan_hanh_trinh"
HOME_BRANCH = "CN_HANH_TRINH"

PERMISSIONS = [
    permission_code(SYSTEM_MODULE, "branch", Action.VIEW),
    permission_code(SYSTEM_MODULE, "branch", Action.CREATE),
    permission_code(SYSTEM_MODULE, "attachment", Action.VIEW),
    permission_code(SYSTEM_MODULE, "attachment", Action.CREATE),
    permission_code(SYSTEM_MODULE, "job", Action.VIEW),
    permission_code(SYSTEM_MODULE, "job", Action.CREATE),
    permission_code(SYSTEM_MODULE, "audit_log", Action.VIEW),
]


@pytest.fixture(scope="module")
def walkthrough_settings(
    test_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Settings:
    return test_settings.model_copy(
        update={"attachments_dir": tmp_path_factory.mktemp("walkthrough-attachments")}
    )


@pytest.fixture
def client(
    walkthrough_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(walkthrough_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def walkthrough_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == WALKTHROUGH_ROLE)) is None:
            role = Role(code=WALKTHROUGH_ROLE, name="Kế toán viên (hành trình)")
            session.add(role)
            session.flush()
            for code in PERMISSIONS:
                permission_id = session.scalar(select(Permission.id).where(Permission.code == code))
                assert permission_id is not None, f"chưa gieo mã quyền {code}"
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
        if session.scalar(select(Branch.id).where(Branch.code == HOME_BRANCH)) is None:
            BranchService(session).create(code=HOME_BRANCH, name="Chi nhánh hành trình")
    return WALKTHROUGH_ROLE


def test_a_days_work_goes_through_every_layer_and_leaves_an_unforgeable_trail(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    walkthrough_role: str,
    app_engine: Engine,
) -> None:
    user = user_factory("hanhtrinh")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=walkthrough_role,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    role_service.assign_branch(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        branch_code=HOME_BRANCH,
        actor_user_id=user.id,
        actor_branch_ids=None,
    )

    # 1. Bắt tay trước khi đăng nhập — màn hình đầu tiên của client thật.
    handshake = client.get("/api/v1/system/handshake")
    assert handshake.status_code == 200
    assert handshake.json()["min_client_version"]

    # 2. Đăng nhập.
    login = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    headers = {
        "Authorization": f"Bearer {login.json()['token']}",
        DATASET_HEADER: dataset_alpha.code,
    }

    # 3. Ghi một bản ghi, có khóa idempotency như mọi lệnh ghi đổi trạng thái.
    branch_code = f"CN_{uuid4().hex[:8].upper()}"
    created = client.post(
        "/api/v1/system/branches",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json={"code": branch_code, "name": "Chi nhánh mới trong hành trình"},
    )
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]

    # 4. Đính một tệp vào chính bản ghi vừa tạo.
    attached = client.post(
        "/api/v1/attachments",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        data={"entity_type": "branches", "entity_id": str(branch_id)},
        files={"file": ("quyet-dinh-thanh-lap.pdf", b"noi dung quyet dinh", "application/pdf")},
    )
    assert attached.status_code == 201, attached.text
    assert (
        client.get(f"/api/v1/attachments/{attached.json()['id']}/content", headers=headers).content
        == b"noi dung quyet dinh"
    )

    # 5. Xếp một tác vụ nền. Không chờ nó chạy: worker là tiến trình riêng, và
    #    điều đang kiểm là API nhận việc rồi trả ngay chứ không tự chạy.
    queued = client.post(
        "/api/v1/jobs", headers=headers, json={"type": SLOW_TASK.code, "params": {"seconds": 1}}
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"

    # 6. Nhật ký thấy đủ cả ba việc vừa làm — và chỉ trong phạm vi chi nhánh của
    #    người gọi (RLS), không phải nhờ một `WHERE` nào trong endpoint.
    entries = client.get("/api/v1/system/audit-log?page_size=200", headers=headers).json()["items"]
    branch_rows = [
        entry
        for entry in entries
        if entry["entity_type"] == "branches" and entry["entity_id"] == str(branch_id)
    ]
    attachment_rows = [entry for entry in entries if entry["entity_type"] == "attachments"]
    assert branch_rows, "lệnh ghi qua HTTP phải để lại vết"
    assert attachment_rows, "đính kèm qua HTTP phải để lại vết"

    # 7. Điểm chốt: dòng nhật ký **của chính hành trình này** không sửa được
    #    bằng vai trò mà app server chạy transaction nghiệp vụ (RT-02). Nếu một
    #    lát nào đó cho transaction chạy bằng vai trò owner "cho tiện", đây là
    #    chỗ nó lộ ra — mọi test khác vẫn xanh.
    #
    #    Đi qua `dataset_session` chứ **không** phải một `Engine.connect()` trần:
    #    từ D3, `ket_app` không có cả `USAGE` trên schema dataset, nên một kết
    #    nối trần dừng ở "permission denied for schema" — chưa chạm tới bảng, và
    #    phép kiểm sẽ xanh mà không chứng minh được gì về `audit_log`.
    #    `dataset_session` `SET LOCAL ROLE ds_<mã>_app`, đúng vai trò mà mọi
    #    request nghiệp vụ chạy dưới.
    audit_id = branch_rows[0]["id"]
    for statement in (
        "UPDATE audit_log SET action = 'created' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
    ):
        with dataset_session(
            session_factory,
            dataset_schema=dataset_alpha.schema_name,
            branch_ids=(),
            audit=AuditContext(user_id=user.id, branch_id=None, client_info="pytest"),
        ) as session:
            with pytest.raises(ProgrammingError) as failure:
                session.execute(text(statement), {"id": audit_id})
        message = str(failure.value).lower()
        assert "permission denied" in message
        # Đối chứng cho chính phép kiểm: lỗi phải nói về **bảng**, không phải về
        # schema. "permission denied for schema" nghĩa là câu lệnh dừng trước khi
        # chạm `audit_log`, và khi ấy test này không kiểm gì cả.
        assert "for schema" not in message, (
            "câu lệnh dừng ở quyền schema, chưa chạm bảng — phép kiểm rỗng"
        )
