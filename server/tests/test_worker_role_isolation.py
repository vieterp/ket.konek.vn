"""Vai trò `ket_worker` chạm được đúng những gì nó cần — không hơn (D2/F2).

Lát 2B-0 đã dựng ranh giới "một dataset một vai trò". Tiến trình chạy tác vụ nền
là **lỗ hổng tự nhiên** trong ranh giới đó: nó phải thấy job của mọi chi nhánh
để giành việc, và nó phải chuyển được sang vai trò của bất kỳ dataset nào để
chạy thân job. Test ở đây khoanh chính xác chỗ lỗ đó rộng tới đâu.

Ba khẳng định, và mất một trong ba là mất cả cơ chế:

1. Worker **thấy** hàng đợi của mọi chi nhánh (nếu không, không job nào chạy).
2. Worker **không** đọc/ghi được bảng nghiệp vụ nào bằng danh nghĩa của chính
   nó — kể cả bảng của dataset mà nó đang xử lý job.
3. Sau `SET ROLE ds_<mã>_app`, ngoại lệ của worker **tắt**: thân job lại chịu
   RLS chi nhánh như một request HTTP bình thường.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import worker_session
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.dataset_roles import WORKER_ROLE

pytestmark = pytest.mark.db

USER_ID = 1


def _scope(dataset: DatasetRef, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=USER_ID, branch_ids=branch_ids)


@pytest.fixture(scope="module")
def branch_id(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> int:
    """Một chi nhánh thật để gắn job vào — job không chi nhánh không chứng minh gì."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        branch = BranchService(session).create(
            code=f"CN_W_{uuid4().hex[:6].upper()}", name="Chi nhánh worker"
        )
        session.flush()
        return branch.id


def test_the_worker_role_cannot_log_in_as_a_privileged_role(worker_engine: Engine) -> None:
    """Kiểm **kết quả** trong `pg_roles`, không tin vào câu lệnh `CREATE ROLE`."""
    with worker_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolinherit "
                "FROM pg_roles WHERE rolname = :name"
            ),
            {"name": WORKER_ROLE},
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False, "BYPASSRLS ở đây làm mọi policy chi nhánh thành trang trí"
    assert row.rolcreatedb is False
    assert row.rolcreaterole is False
    assert row.rolinherit is False, (
        "NOINHERIT là bản lề: kế thừa tự động nghĩa là worker có sẵn quyền của mọi dataset"
    )


def test_the_worker_sees_queued_jobs_of_every_branch(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    branch_id: int,
) -> None:
    """Chính là lý do policy `p_worker_queue` tồn tại.

    Không có nó, policy chi nhánh giấu mọi job có `branch_id` khỏi worker — hàng
    đợi trông như rỗng và không có gì báo lỗi.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha, (branch_id,))) as session:
        job = queue.enqueue(
            session,
            job_type=SLOW_TASK,
            params={"seconds": 1},
            requested_by=USER_ID,
            branch_id=branch_id,
        )
        job_id = job.id

    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        found = queue.get_job(ws, job_id)
        assert found.branch_id == branch_id


def test_the_worker_cannot_read_business_tables_under_its_own_identity(
    worker_session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Quyền của worker dừng ở đúng bảng `jobs`.

    Nó chạy mã của mọi phase sau, nên mỗi bảng thừa trong tầm với của nó là một
    nấc leo thang sẵn có nếu một thân job bị lợi dụng.
    """
    for table in ("branches", "audit_log", "settings", "idempotency_keys", "user_roles"):
        with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
            with pytest.raises(ProgrammingError) as exc:
                ws.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            assert "permission denied" in str(exc.value).lower(), table


def test_the_worker_cannot_reach_the_control_schema_identity_tables(
    worker_session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Worker không xác thực ai cả — `users`/`auth_sessions` nằm ngoài tầm với.

    Nếu nó đọc được, một lỗ hổng trong thân job (mã của mọi phase sau) sẽ đọc
    được băm mật khẩu và token phiên của cả bản cài.
    """
    for table in ("users", "auth_sessions", "control_audit_log"):
        with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
            with pytest.raises(ProgrammingError) as exc:
                ws.execute(text(f"SELECT 1 FROM public.{table} LIMIT 1"))
            assert "permission denied" in str(exc.value).lower(), table


def test_the_worker_cannot_insert_jobs_only_run_them(
    worker_session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Xếp hàng là việc của người dùng qua API.

    Một worker tự sinh job cho chính mình là vòng lặp không ai duyệt, và nó cũng
    là cách một thân job bị lợi dụng tự nhân bản.
    """
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        with pytest.raises(ProgrammingError) as exc:
            ws.execute(
                text(
                    "INSERT INTO jobs (id, type, status, progress, requested_by, "
                    "resume_semantics) VALUES (gen_random_uuid(), 'x', 'queued', 0, 1, "
                    "'idempotent-restart')"
                )
            )
        assert "permission denied" in str(exc.value).lower()


def test_the_worker_cannot_delete_jobs(
    worker_session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Dòng job là lịch sử "đã chạy gì lúc nào" — worker không xóa được nó."""
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        with pytest.raises(ProgrammingError) as exc:
            ws.execute(text("DELETE FROM jobs"))
        assert "permission denied" in str(exc.value).lower()


def test_the_worker_exception_switches_off_after_set_role(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    branch_id: int,
) -> None:
    """Sau `SET ROLE`, thân job chịu RLS chi nhánh như một request bình thường.

    Đây là nửa còn lại của quyết định F2: ngoại lệ chỉ rộng bằng đúng lượt giành
    việc. Với phạm vi chi nhánh **rỗng**, một job có `branch_id` phải biến mất
    khỏi tầm nhìn — fail-closed, cùng hướng với mọi chỗ khác của RLS.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha, (branch_id,))) as session:
        job = queue.enqueue(
            session,
            job_type=SLOW_TASK,
            params={"seconds": 1},
            requested_by=USER_ID,
            branch_id=branch_id,
        )
        job_id = job.id

    role = role_name_for_schema(dataset_alpha.schema_name)
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        assert queue.get_job(ws, job_id) is not None
        ws.execute(text(f"SET LOCAL ROLE {role}"))
        ws.expire_all()
        visible = ws.execute(
            text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
        assert visible == 0, (
            "sau SET ROLE, policy TO ket_worker không còn áp — job của chi nhánh khác "
            "phải vô hình với phạm vi rỗng"
        )


def test_the_worker_cannot_rewrite_what_a_job_is_supposed_to_do(
    session_factory: sessionmaker[Session],
    worker_session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Quyền `UPDATE` của worker dừng ở **cột cơ chế** (H4).

    Đo được trước khi siết: với `GRANT UPDATE` toàn bảng, một câu lệnh dưới danh
    nghĩa `ket_worker` đổi được `type` của một job đang chờ thành loại cần kết
    nối đặc quyền, đổi `params`, đổi `requested_by` — và worker **không kiểm lại
    quyền** lúc chạy, nên dòng job bị viết lại sẽ chạy như thật.

    Ranh giới: worker ghi được *về chính lượt chạy của mình* (trạng thái, tiến
    độ, kết quả, lease), không ghi được *việc phải làm* hay *ai yêu cầu*.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.enqueue(
            session, job_type=SLOW_TASK, params={"seconds": 1}, requested_by=USER_ID
        )
        job_id = job.id

    forbidden = {
        "type": "'system.maintenance.prune_sessions'",
        "params": "'{}'::jsonb",
        "requested_by": "999",
        "branch_id": "4242",
        "cancel_requested": "true",
    }
    for column, value in forbidden.items():
        with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
            with pytest.raises(ProgrammingError) as exc:
                ws.execute(
                    text(f"UPDATE jobs SET {column} = {value} WHERE id = :id"), {"id": job_id}
                )
            assert "permission denied" in str(exc.value).lower(), column

    # Đối chứng: cột cơ chế thì ghi được — nếu không, worker không chạy được job
    # nào và test trên xanh vì lý do sai.
    with worker_session(worker_session_factory, dataset_schema=dataset_alpha.schema_name) as ws:
        ws.execute(text("UPDATE jobs SET progress = 5 WHERE id = :id"), {"id": job_id})

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        job = queue.get_job(session, job_id)
        assert job.progress == 5
        assert job.type == SLOW_TASK.code
        assert job.requested_by == USER_ID
