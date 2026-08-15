"""Hàng đợi tác vụ nền qua HTTP: quyền, mã trạng thái, hợp đồng lỗi.

Test hàng đợi kiểm cơ chế; ở đây kiểm **những gì client thấy** — một cơ chế
đúng ở tầng dịch vụ nhưng nối sai vào endpoint chỉ lộ ra ở mức này.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.api.dependencies import DATASET_HEADER
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import JOB_CANCEL, JOB_CREATE, JOB_VIEW, MAINTENANCE_RUN
from ket.kernel.jobs.models import Job, JobStatus
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Permission, Role, RolePermission
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

OPERATOR_ROLE = "van_hanh_tac_vu"
"""Xem + xếp hàng + hủy tác vụ, **không** có quyền chạy tác vụ dọn dẹp.

Đó là ranh giới mà nửa số test dưới đây kiểm: dùng được hàng đợi ≠ được chạy
mọi việc trong đó."""

MAINTAINER_ROLE = "quan_tri_don_dep"
VIEWER_ROLE = "chi_xem_tac_vu"

SLOW_TASK_CODE = "system.diagnostic.slow_task"
PRUNE_KEYS_CODE = "system.maintenance.prune_idempotency_keys"


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    app = create_app(test_settings)
    with TestClient(app, raise_server_exceptions=False) as instance:
        yield instance


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


@pytest.fixture(autouse=True)
def empty_queue(drain_jobs: Callable[..., None], dataset_alpha: DatasetRef) -> None:
    drain_jobs(dataset_alpha)


def _ensure_role(
    session_factory: sessionmaker[Session], dataset: DatasetRef, code: str, permissions: list[str]
) -> str:
    with unit_of_work(session_factory, _scope(dataset)) as session:
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            role = Role(code=code, name=f"Vai trò {code}")
            session.add(role)
            session.flush()
            for permission in permissions:
                permission_id = session.scalar(
                    select(Permission.id).where(Permission.code == permission)
                )
                assert permission_id is not None, f"chưa gieo mã quyền {permission}"
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return code


@pytest.fixture(scope="module")
def operator_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(
        session_factory, dataset_alpha, OPERATOR_ROLE, [JOB_VIEW, JOB_CREATE, JOB_CANCEL]
    )


@pytest.fixture(scope="module")
def maintainer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(
        session_factory,
        dataset_alpha,
        MAINTAINER_ROLE,
        [JOB_VIEW, JOB_CREATE, MAINTENANCE_RUN],
    )


@pytest.fixture(scope="module")
def viewer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(session_factory, dataset_alpha, VIEWER_ROLE, [JOB_VIEW])


def _actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role_code: str,
    prefix: str,
) -> dict[str, str]:
    """Tạo người dùng có vai trò, đăng nhập, trả headers dùng được ngay."""
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
    )
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['token']}",
        DATASET_HEADER: dataset.code,
    }


# --------------------------------------------------------------------------
# Xếp hàng
# --------------------------------------------------------------------------


def test_enqueueing_returns_202_and_a_queued_job(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """`202` chứ không `201`: yêu cầu đã ghi nhận, kết quả thì chưa có."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "xephang")

    response = client.post(
        "/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE, "params": {"seconds": 2}}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == JobStatus.QUEUED.value
    assert body["progress"] == 0
    assert body["resume_semantics"] == "idempotent-restart"


def test_enqueueing_needs_no_idempotency_key(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """FR-NFR-004 miễn trừ `/jobs` tường minh — và cổng chống-mục-nát biết điều đó.

    Xếp hàng tạo một *yêu cầu*, không ghi dữ liệu kế toán; bấm hai lần chỉ tạo
    hai lượt chạy của một việc vốn phải chịu được chạy lại.
    """
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, operator_role, "khongkhoa"
    )

    response = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE})

    assert response.status_code == 202, response.text


def test_a_job_type_outside_the_registry_is_a_422_not_a_500(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "lala")

    response = client.post("/api/v1/jobs", headers=headers, json={"type": "khong.he.co"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "job.type_unknown"


def test_params_that_do_not_fit_the_type_are_refused_before_queueing(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """Sai tham số hỏng ở lượt bấm nút, không phải vài phút sau ở tiến trình khác."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "saitham")

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"type": SLOW_TASK_CODE, "params": {"seconds": 100_000}},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "job.params_invalid"
    assert body["details"]["field"] == "seconds"

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert session.scalars(select(Job)).all() == []


def test_using_the_queue_does_not_grant_the_right_to_run_maintenance(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """Hai lớp quyền, hai câu hỏi khác nhau.

    Người vận hành có `system.job.create` (dùng được hàng đợi) nhưng không có
    `system.maintenance.create` — không lớp thứ hai thì ai xếp được hàng cũng
    xóa được phiên đăng nhập của cả bản cài.
    """
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, operator_role, "vuottham"
    )

    response = client.post("/api/v1/jobs", headers=headers, json={"type": PRUNE_KEYS_CODE})

    assert response.status_code == 403
    assert response.json()["details"]["permission"] == MAINTENANCE_RUN


def test_a_maintainer_can_enqueue_the_maintenance_job(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    maintainer_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, maintainer_role, "bao")

    response = client.post("/api/v1/jobs", headers=headers, json={"type": PRUNE_KEYS_CODE})

    assert response.status_code == 202, response.text


def test_viewing_alone_does_not_allow_queueing(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    viewer_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, viewer_role, "chixem")

    response = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE})

    assert response.status_code == 403
    assert response.json()["details"]["permission"] == JOB_CREATE


# --------------------------------------------------------------------------
# Đọc + hủy
# --------------------------------------------------------------------------


def test_a_job_can_be_read_back_and_listed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "docl")
    job_id = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE}).json()[
        "id"
    ]

    single = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    listed = client.get("/api/v1/jobs", headers=headers, params={"status": "queued"})

    assert single.status_code == 200
    assert single.json()["type"] == SLOW_TASK_CODE
    assert job_id in [item["id"] for item in listed.json()["items"]]


def test_an_unknown_status_filter_is_refused_instead_of_returning_an_empty_queue(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """`?status=runing` trả danh sách rỗng thì client đọc thành "hàng đợi trống"."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "loc")

    response = client.get("/api/v1/jobs", headers=headers, params={"status": "runing"})

    assert response.status_code == 422


def test_a_job_of_another_dataset_is_not_readable(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """Hàng đợi cũng là dữ liệu của **một** doanh nghiệp (ADR-017)."""
    headers = _actor(
        client, session_factory, dataset_alpha, user_factory, operator_role, "chinhanh"
    )
    job_id = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE}).json()[
        "id"
    ]

    response = client.get(f"/api/v1/jobs/{job_id}", headers={**headers, DATASET_HEADER: "beta"})

    # Chốt **mã cụ thể**, không phải "403 hoặc 404": hai mã trả lời hai câu hỏi
    # khác nhau và ở đây câu trả lời đúng là câu **sớm hơn** — người dùng không
    # có vai trò nào trong dữ liệu kế toán kia, nên request dừng ở cổng dataset
    # trước khi có ai đi tìm job. (Trường hợp người dùng có quyền ở **cả hai**
    # dataset thì dừng ở `job.not_found` — hàng đợi nằm trong schema dataset.)
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "dataset.access_denied"


def test_a_missing_job_is_a_404(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "khongco")

    response = client.get(f"/api/v1/jobs/{uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "job.not_found"


def test_cancelling_records_the_request_without_promising_it_stopped(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    """Hủy là **yêu cầu** gửi cho worker, không phải lệnh giết."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "huy")
    job_id = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE}).json()[
        "id"
    ]

    response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["cancel_requested"] is True
    assert response.json()["status"] == JobStatus.QUEUED.value, "chưa dừng — mới ghi nhận"


def test_cancelling_needs_its_own_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    maintainer_role: str,
) -> None:
    """Vai trò bảo trì xếp được hàng nhưng không hủy được việc của người khác."""
    headers = _actor(client, session_factory, dataset_alpha, user_factory, maintainer_role, "kohuy")
    job_id = client.post("/api/v1/jobs", headers=headers, json={"type": PRUNE_KEYS_CODE}).json()[
        "id"
    ]

    response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)

    assert response.status_code == 403
    assert response.json()["details"]["permission"] == JOB_CANCEL


def test_cancelling_a_finished_job_is_a_409(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
) -> None:
    headers = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "xong")
    job_id = client.post("/api/v1/jobs", headers=headers, json={"type": SLOW_TASK_CODE}).json()[
        "id"
    ]
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        # Đường API không giành lease; ép trạng thái kết thúc bằng chính đường
        # worker đi qua (giành rồi kết thúc) để test dùng đúng hợp đồng thật.
        job = queue.get_job(session, UUID(job_id))
        job.status = JobStatus.DONE.value
        job.finished_at = datetime.now(UTC)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)

    assert response.status_code == 409
    assert response.json()["error_code"] == "job.not_cancellable"


# --------------------------------------------------------------------------
# Danh mục loại tác vụ
# --------------------------------------------------------------------------


def test_the_type_list_only_shows_what_the_caller_may_run(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    operator_role: str,
    maintainer_role: str,
) -> None:
    """Màn hình không được hiện một nút chắc chắn trả `403`."""
    operator = _actor(client, session_factory, dataset_alpha, user_factory, operator_role, "dsvh")
    maintainer = _actor(
        client, session_factory, dataset_alpha, user_factory, maintainer_role, "dsbt"
    )

    operator_codes = {
        item["code"] for item in client.get("/api/v1/jobs/types", headers=operator).json()["items"]
    }
    maintainer_codes = {
        item["code"]
        for item in client.get("/api/v1/jobs/types", headers=maintainer).json()["items"]
    }

    assert SLOW_TASK_CODE in operator_codes
    assert PRUNE_KEYS_CODE not in operator_codes
    assert PRUNE_KEYS_CODE in maintainer_codes


def test_a_dataset_maintainer_does_not_see_installation_wide_jobs(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    maintainer_role: str,
) -> None:
    """Ranh giới của H3: quyền cấp **dataset** không mở khóa việc cấp **bản cài**.

    `role_permissions` nằm trong schema của từng dữ liệu kế toán, nên nếu dọn
    phiên đăng nhập dùng chung mã quyền với dọn khóa idempotency thì người chỉ
    quản trị doanh nghiệp B xóa được lịch sử đăng nhập của mọi người — kể cả
    người chưa từng mở B. Nay nó đòi `system.installation.create` riêng.

    Cờ `requires_privileged_connection` của loại job đó được canh ở tầng
    registry (`test_job_registry.py`), không phải ở đây: quyền cấp bản cài đòi
    2FA, nên dựng một người gọi có nó qua HTTP cần cả vòng đăng ký thiết bị —
    một phép kiểm đắt hơn nhiều so với thứ nó chứng minh thêm.
    """
    headers = _actor(client, session_factory, dataset_alpha, user_factory, maintainer_role, "dacq")

    items = {
        item["code"]: item
        for item in client.get("/api/v1/jobs/types", headers=headers).json()["items"]
    }

    assert "system.maintenance.prune_sessions" not in items
    assert items[PRUNE_KEYS_CODE]["requires_privileged_connection"] is False
