"""Vòng lặp của tiến trình chạy tác vụ nền (ADR-014, FR-NFR-042/044).

Một vòng lặp, bốn việc, theo đúng thứ tự này cho **từng** dữ liệu kế toán:

1. Quét job hết lease (reaper — RT-13), thưa hơn nhịp giành việc.
2. Giành một job (`FOR UPDATE SKIP LOCKED`) dưới danh nghĩa `ket_worker`.
3. Chạy thân job dưới `SET LOCAL ROLE ds_<mã>_app` + phạm vi chi nhánh lấy từ
   `jobs.branch_id` — tức là dưới đúng bộ quyền và đúng RLS của một request.
4. Ghi kết quả (hoặc lỗi, hoặc trạng thái đã hủy) trong một transaction riêng.

**Vì sao tiến trình riêng chứ không một luồng trong FastAPI:** GIL. Một job
tính lại giá xuất kho chiếm CPU hàng phút; chạy trong tiến trình API thì mọi
request khác xếp hàng sau nó và FR-NFR-044 ("giao diện không bị khóa") hỏng
ngay ở job đầu tiên.

**Reaper nằm trong chính vòng lặp này** (quyết định của lát 2B-2b) chứ không
phải một tiến trình thứ ba: bản cài đích do người không phải IT cài (LD-01), và
mỗi dịch vụ phải cài đúng là một điểm hỏng nữa. Đánh đổi được ghi ra: chết hết
worker thì không ai reap — nhưng lúc đó cũng không ai chạy job, nên job "kẹt"
không tệ hơn hàng đợi đứng.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.datasets.service import list_datasets
from ket.kernel.errors import DomainError, JobPrivilegeUnavailableError
from ket.kernel.jobs import queue
from ket.kernel.jobs.models import Job
from ket.kernel.jobs.reaper import reap_expired_leases
from ket.kernel.jobs.registry import (
    REGISTRY,
    JobCancelled,
    JobContext,
    JobPrivilege,
    JobProgress,
    JobRegistry,
    JobResult,
    JobType,
)
from ket.kernel.logging_setup import get_logger
from ket.kernel.persistence.session import control_session, worker_session
from ket.kernel.persistence.types import JsonPrimitive
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.settings import Settings
from ket.worker.progress import LeaseLostError, WorkerProgress

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClaimedJob:
    """Ảnh chụp dòng job **ngay sau khi giành**, tách khỏi `Session` đã đóng.

    Cần vì transaction giành việc kết thúc trước khi thân job chạy: đọc thuộc
    tính của một đối tượng ORM đã hết phiên sẽ ném `DetachedInstanceError` giữa
    lúc chạy job — và nó ném ở nơi khó đoán nhất, khi một thuộc tính chưa được
    nạp bị chạm lần đầu.
    """

    job_id: UUID
    type: str
    params: dict[str, JsonPrimitive]
    branch_id: int | None
    requested_by: int
    attempt: int
    """Số hiệu lượt chạy — **hàng rào** cho mọi lệnh ghi trạng thái (xem
    `queue._fenced_update`), không phải số liệu thống kê."""

    @classmethod
    def of(cls, job: Job) -> ClaimedJob:
        return cls(
            job_id=job.id,
            type=job.type,
            params=dict(job.params or {}),
            branch_id=job.branch_id,
            requested_by=job.requested_by,
            attempt=job.attempt,
        )


class Worker:
    """Tiến trình chạy tác vụ nền. Một đối tượng để test lái được từng nhịp."""

    def __init__(
        self,
        settings: Settings,
        *,
        worker_factory: sessionmaker[Session],
        owner_factory: sessionmaker[Session] | None = None,
        engine: Engine,
        registry: JobRegistry = REGISTRY,
    ) -> None:
        self._settings = settings
        self._factory = worker_factory
        self._owner_factory = owner_factory
        self._engine = engine
        self._registry = registry
        self._stop = threading.Event()
        self._lease = timedelta(seconds=settings.job_lease_seconds)
        self._heartbeat = timedelta(seconds=settings.job_heartbeat_seconds)
        self._reaped_at: dict[str, datetime] = {}

    # --- vòng đời ---------------------------------------------------------

    def stop(self) -> None:
        """Yêu cầu dừng sau job hiện tại (drain — RT-13/RT-14).

        Dừng **giữa** hai job chứ không giữa một job: nâng cấp cắt ngang một bút
        toán phân bổ để lại sổ lệch, và installer chờ thêm vài giây thì rẻ hơn
        nhiều so với một lần khôi phục.
        """
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """`SIGINT`/`SIGTERM` → drain, không giết ngay.

        `SIGTERM` là thứ Windows Service Manager và `systemd` gửi lúc dừng dịch
        vụ, nên đây chính là đường đi của mọi lần nâng cấp có kế hoạch.
        """

        def handle(signum: int, _frame: FrameType | None) -> None:
            logger.info("worker_stop_requested", signal=signum)
            self.stop()

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)

    def run_forever(self) -> None:
        """Chạy tới khi được yêu cầu dừng."""
        logger.info(
            "worker_started",
            poll_seconds=self._settings.worker_poll_seconds,
            lease_seconds=self._settings.job_lease_seconds,
        )
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                # **Ranh giới lỗi bắt buộc.** `run_once` chạm DB ở ba chỗ nằm
                # ngoài thân job (đọc sổ đăng ký dữ liệu kế toán, quét lease,
                # giành việc); một lần PostgreSQL khởi động lại hay mạng chớp sẽ
                # ném `OperationalError` qua cả vòng lặp. Không bắt thì **tiến
                # trình worker chết im lặng**, và trên bản cài do người không
                # phải IT vận hành (LD-01) hàng đợi đứng cho tới khi ai đó để ý
                # — triệu chứng đầu tiên là "bấm tính giá thành mãi không thấy
                # gì". Bắt rộng có chủ đích: mọi lỗi ngoài thân job đều là lỗi
                # hạ tầng nhất thời hoặc lỗi lập trình, và cả hai đều không đáng
                # để giết một dịch vụ chạy nền.
                logger.exception("worker_cycle_failed")
                self._stop.wait(self._settings.worker_error_backoff_seconds)
                continue
            if not worked:
                # `Event.wait` chứ không `time.sleep`: tín hiệu dừng đánh thức
                # ngay, nên `systemctl stop` không phải chờ hết một nhịp nghỉ.
                self._stop.wait(self._settings.worker_poll_seconds)
        logger.info("worker_stopped")

    def run_once(self) -> bool:
        """Một vòng qua mọi dữ liệu kế toán. Trả `True` nếu có job nào được chạy.

        Lỗi của một dataset được cô lập tại đây; lỗi của chính lượt đọc sổ đăng
        ký thì nổi lên cho `run_forever` xử lý (nó là lỗi của cả vòng).
        """
        worked = False
        for dataset in self._datasets():
            if self._stop.is_set():
                break
            try:
                self._reap_if_due(dataset)
                if self.run_next(dataset):
                    worked = True
            except Exception:
                # Lỗi ở **một** dữ liệu kế toán không được dừng cả vòng quét:
                # một schema đang khôi phục dở (chưa có bảng `jobs`, chưa cấp
                # lại quyền) sẽ chặn mọi dataset đứng sau nó trong danh sách.
                logger.exception("dataset_cycle_failed", dataset=dataset.code)
        return worked

    # --- các bước ---------------------------------------------------------

    def _datasets(self) -> Sequence[DatasetRef]:
        """Danh sách dữ liệu kế toán đang hoạt động.

        Đọc lại **mỗi vòng**: một dataset tạo lúc 9 giờ sáng phải chạy được job
        mà không cần khởi động lại dịch vụ worker.

        `include_inactive=False`: dữ liệu kế toán đã ngừng hoạt động không được
        tự chạy tác vụ nền — job còn treo trong đó chờ tới khi nó được bật lại.
        """
        return list_datasets(self._engine)

    def _reap_if_due(self, dataset: DatasetRef) -> None:
        """Quét job hết lease, thưa hơn nhịp giành việc."""
        now = datetime.now(UTC)
        last = self._reaped_at.get(dataset.schema_name)
        if last is not None and now - last < timedelta(seconds=self._settings.worker_reap_seconds):
            return
        self._reaped_at[dataset.schema_name] = now
        with worker_session(self._factory, dataset_schema=dataset.schema_name) as session:
            outcome = reap_expired_leases(
                session, max_attempts=self._settings.job_max_attempts, now=now
            )
        if outcome.touched():
            logger.warning(
                "jobs_reaped",
                dataset=dataset.code,
                requeued=outcome.requeued,
                failed=outcome.failed,
            )

    def run_next(self, dataset: DatasetRef) -> bool:
        """Giành và chạy **một** job của dataset. `False` nếu hàng đợi rỗng."""
        with worker_session(self._factory, dataset_schema=dataset.schema_name) as session:
            job = queue.claim_next(session, lease=self._lease)
            claimed = ClaimedJob.of(job) if job is not None else None
        if claimed is None:
            return False

        logger.info(
            "job_claimed",
            dataset=dataset.code,
            job_id=str(claimed.job_id),
            job_type=claimed.type,
            attempt=claimed.attempt,
        )
        self._execute(dataset, claimed)
        return True

    def _execute(self, dataset: DatasetRef, claimed: ClaimedJob) -> None:
        """Chạy thân job và ghi kết quả — **mọi** đường ra đều kết thúc dòng job.

        Đây là bất biến quan trọng nhất của hàm: một ngoại lệ không bắt ở đây để
        lại job "đang chạy" cho tới khi lease hết hạn, tức là biến một lỗi lập
        trình thành một job mồ côi mất một phút mới lộ ra.
        """
        progress = WorkerProgress(
            self._factory,
            job_id=claimed.job_id,
            dataset_schema=dataset.schema_name,
            lease=self._lease,
            attempt=claimed.attempt,
        )
        try:
            job_type = self._registry.get(claimed.type)
            result = self._run_body(dataset, claimed, job_type, progress)
        except LeaseLostError as exc:
            # Job đã thuộc về lượt chạy khác — **không** ghi gì lên dòng đó nữa.
            logger.warning("job_lease_lost", job_id=str(claimed.job_id), error=str(exc))
            return
        except JobCancelled:
            # Thân job đã dừng ở ranh giới lô và transaction của nó đã rollback,
            # nên không có việc dở dang nào được commit.
            with worker_session(self._factory, dataset_schema=dataset.schema_name) as session:
                queue.mark_cancelled(session, claimed.job_id, attempt=claimed.attempt)
            logger.info("job_cancelled", job_id=str(claimed.job_id))
            return
        except DomainError as exc:
            self._finalize_failure(dataset, claimed, f"[{exc.error_code}] {exc}")
            return
        except Exception as exc:
            # Không để ngoại lệ nào giết vòng lặp: một job hỏng phải là một job
            # hỏng, không phải một tiến trình worker chết và mọi job còn lại
            # đứng im. Chi tiết đi vào log (có traceback), thông điệp trên dòng
            # job giữ ngắn cho người vận hành đọc.
            logger.exception("job_failed", job_id=str(claimed.job_id), job_type=claimed.type)
            self._finalize_failure(
                dataset, claimed, f"Tác vụ dừng vì lỗi không mong muốn: {type(exc).__name__}"
            )
            return

        with worker_session(self._factory, dataset_schema=dataset.schema_name) as session:
            recorded = queue.finish(session, claimed.job_id, attempt=claimed.attempt, result=result)
        if not recorded:
            # Lease mất **sau** khi thân job chạy xong. Không ghi đè: dòng job
            # giờ thuộc về lượt chạy khác, và kết quả ở đây mô tả một lượt chạy
            # mà người dùng không còn nhìn thấy.
            logger.warning("job_result_dropped_lease_lost", job_id=str(claimed.job_id))
            return
        logger.info("job_done", job_id=str(claimed.job_id), job_type=claimed.type)

    def _run_body(
        self,
        dataset: DatasetRef,
        claimed: ClaimedJob,
        job_type: JobType[Any],
        progress: JobProgress,
    ) -> JobResult:
        """Mở transaction đúng loại cho thân job rồi gọi nó.

        Hai loại, khác nhau ở **vai trò DB** (xem `JobPrivilege`):

        * `DATASET` — `unit_of_work` chuẩn: `SET LOCAL ROLE ds_<mã>_app`,
          `search_path`, và GUC chi nhánh đặt theo `jobs.branch_id`. Job vì thế
          bị RLS chặn đúng như request của người đã yêu cầu nó.
        * `CONTROL_OWNER` — `control_session` trên engine `ket_owner` **nếu** bản
          cài cấu hình tường minh; không thì từ chối, không âm thầm hạ rào.
        """
        if job_type.privilege is JobPrivilege.CONTROL_OWNER:
            if self._owner_factory is None:
                raise JobPrivilegeUnavailableError(
                    "Tác vụ này cần kết nối đặc quyền mà tiến trình chạy tác vụ nền không có. "
                    "Cấu hình KET_WORKER_OWNER_DATABASE_URL cho worker, hoặc chạy lệnh tương "
                    "ứng của `python -m ket.admin` tại máy chủ.",
                    job_type=claimed.type,
                )
            with control_session(self._owner_factory) as session:
                return self._call(job_type, session, dataset, claimed, progress)

        scope = RequestScope(
            dataset_schema=dataset.schema_name,
            user_id=claimed.requested_by,
            # Phạm vi chi nhánh của job = đúng chi nhánh ghi trên dòng job. Job
            # không mang chi nhánh (tác vụ mức dữ liệu kế toán) chạy với phạm vi
            # rỗng, tức là chỉ thấy dòng `branch_id IS NULL` — fail-closed,
            # cùng hướng với mọi chỗ khác của RLS.
            branch_ids=(claimed.branch_id,) if claimed.branch_id is not None else (),
            client_info=f"ket.worker job={claimed.job_id}",
            acting_branch_id=claimed.branch_id,
        )
        with unit_of_work(self._factory, scope) as session:
            return self._call(job_type, session, dataset, claimed, progress)

    def _call(
        self,
        job_type: JobType[Any],
        session: Session,
        dataset: DatasetRef,
        claimed: ClaimedJob,
        progress: JobProgress,
    ) -> JobResult:
        """Dựng `JobContext` rồi gọi thân job; chặn trước nếu đã có yêu cầu hủy.

        Kiểm cờ hủy **trước** khi chạy: người dùng bấm Hủy trong lúc job còn xếp
        hàng là trường hợp thường gặp nhất, và chạy trọn 30 giây một việc đã bị
        hủy là cách chắc chắn để họ không tin nút đó nữa.
        """
        if progress.cancel_requested():
            raise JobCancelled(f"Tác vụ {claimed.job_id} bị hủy trước khi bắt đầu")
        context = JobContext(
            job_id=claimed.job_id,
            session=session,
            progress=progress,
            attempt=claimed.attempt,
            dataset_schema=dataset.schema_name,
            branch_id=claimed.branch_id,
            requested_by=claimed.requested_by,
        )
        return job_type.run(context, claimed.params)

    def _finalize_failure(self, dataset: DatasetRef, claimed: ClaimedJob, message: str) -> None:
        """Ghi trạng thái hỏng bằng một transaction **mới**.

        Transaction của thân job đã rollback lúc ngoại lệ nổi lên; ghi tiếp vào
        nó sẽ không lưu được gì và job lại thành mồ côi.
        """
        with worker_session(self._factory, dataset_schema=dataset.schema_name) as session:
            recorded = queue.fail(session, claimed.job_id, attempt=claimed.attempt, message=message)
        if not recorded:
            logger.warning("job_failure_dropped_lease_lost", job_id=str(claimed.job_id))
