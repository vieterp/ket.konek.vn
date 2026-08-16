"""Job nặng chạy mà giao diện vẫn phản hồi — FR-NFR-042/044.

Đây là tiêu chí phát hành, không phải một phép đo cho vui: cả lý do worker là
**tiến trình riêng** (ADR-014) nằm ở kết quả của test này. Nếu job chạy trong
tiến trình API, GIL sẽ giữ vòng lặp sự kiện và mọi request xếp hàng sau nó.

Cách đo: một job 600 giây đang chạy ở luồng khác (đứng thay cho tiến trình
worker), trong lúc đó bắn 20 request **có chạm DB** và lấy độ trễ lớn nhất.

Ngưỡng 200ms là ngưỡng của FR-NFR-042. Máy CI chậm hơn máy lập trình, nên phép
đo dùng độ trễ **trung vị** cho khẳng định chính và chỉ ghi lại giá trị lớn
nhất: một lần dừng GC 300ms trên runner dùng chung không phải thứ test này nói
về, và một test đỏ ngẫu nhiên là test sẽ bị tắt.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.dependencies import DATASET_HEADER
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import JobNotFoundError
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import JOB_CREATE, JOB_VIEW, SLOW_TASK
from ket.kernel.jobs.models import JobStatus
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Permission, Role, RolePermission
from ket.main import create_app
from ket.settings import Settings
from ket.worker.runner import Worker

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

RESPONSIVENESS_ROLE = "do_do_phan_hoi"
SAMPLES = 20
MEDIAN_BUDGET = timedelta(milliseconds=200)
"""Ngưỡng FR-NFR-042 cho một request đơn giản trong lúc có job nặng."""


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    app = create_app(test_settings)
    with api_test_client(app) as instance:
        yield instance


@pytest.fixture
def headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    drain_jobs: Callable[..., None],
) -> dict[str, str]:
    # `drain_jobs` (chạy bằng `ket_owner`) chứ không `delete(Job)` dưới phạm vi
    # chi nhánh rỗng: cách thứ hai chỉ xóa dòng `branch_id IS NULL`, nên một job
    # còn sót của chi nhánh khác được `claim_next` giành **trước** job của test
    # này — và test đo độ phản hồi đỏ ngẫu nhiên (đo được: 2/4 lần).
    drain_jobs(dataset_alpha)
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        if session.scalar(select(Role.id).where(Role.code == RESPONSIVENESS_ROLE)) is None:
            role = Role(code=RESPONSIVENESS_ROLE, name="Đo độ phản hồi")
            session.add(role)
            session.flush()
            for code in (JOB_VIEW, JOB_CREATE):
                permission_id = session.scalar(select(Permission.id).where(Permission.code == code))
                assert permission_id is not None
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )

    user = user_factory("dophanhoi")
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset_alpha.schema_name,
        user_id=user.id,
        role_code=RESPONSIVENESS_ROLE,
        actor_user_id=user.id,
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {
        "Authorization": f"Bearer {login.json()['token']}",
        DATASET_HEADER: dataset_alpha.code,
    }


def test_the_api_stays_responsive_while_a_long_job_runs(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    test_settings: Settings,
    worker_session_factory: sessionmaker[Session],
    worker_engine: Engine,
    headers: dict[str, str],
) -> None:
    enqueued = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"type": SLOW_TASK.code, "params": {"seconds": 600}},
    )
    assert enqueued.status_code == 202, enqueued.text
    job_id = UUID(enqueued.json()["id"])

    worker = Worker(
        test_settings,
        worker_factory=worker_session_factory,
        owner_factory=None,
        engine=worker_engine,
    )
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()

    try:
        _wait_until_running(client, headers, job_id)

        latencies: list[float] = []
        for _ in range(SAMPLES):
            started = time.perf_counter()
            response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            latencies.append(time.perf_counter() - started)
            assert response.status_code == 200

        median = statistics.median(latencies)
        assert median < MEDIAN_BUDGET.total_seconds(), (
            f"trung vị {median * 1000:.0f}ms vượt ngưỡng FR-NFR-042 "
            f"({MEDIAN_BUDGET.total_seconds() * 1000:.0f}ms) trong lúc job nặng chạy; "
            f"lớn nhất {max(latencies) * 1000:.0f}ms"
        )

        progressing = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        assert progressing["status"] == JobStatus.RUNNING.value
        assert progressing["progress"] > 0, "thanh tiến độ phải nhúc nhích trong lúc job chạy"
    finally:
        # `suppress`: nhánh dọn dẹp **không được** ném đè lên lỗi thật của test.
        # Trước đây `request_cancel` ném `JobNotFoundError` từ trong `finally`,
        # và người đọc CI thấy một lỗi hoàn toàn sai chỗ thay vì nguyên nhân.
        with (
            suppress(JobNotFoundError),
            unit_of_work(session_factory, _scope(dataset_alpha)) as session,
        ):
            queue.request_cancel(session, job_id)
        thread.join(timeout=15)


def _wait_until_running(client: TestClient, headers: dict[str, str], job_id: UUID) -> None:
    """Chờ worker nhận job, **có kiểm mã trạng thái**.

    Thân RFC 7807 cũng có trường `status` (là số HTTP), nên một phản hồi `404`
    đọc thành `body["status"] == 404` — vòng chờ im lặng đủ 15 giây rồi báo
    "worker không nhận job", tức là chẩn đoán sai hẳn nguyên nhân.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=15)
    while datetime.now(UTC) < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] == JobStatus.RUNNING.value and body["progress"] > 0:
            return
        time.sleep(0.1)
    raise AssertionError("worker không nhận job trong 15 giây")
