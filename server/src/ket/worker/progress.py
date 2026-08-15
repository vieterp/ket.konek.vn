"""Kênh tiến độ + cờ hủy, ghi bằng **connection riêng** của worker.

Đây là chi tiết dễ làm sai nhất của cả tiến trình worker, nên nó có tệp riêng.

Tiến độ **không được** ghi trong transaction nghiệp vụ của job: transaction đó
mở suốt thời gian job chạy (có khi vài phút), và mọi thứ ghi trong nó chỉ nhìn
thấy được sau khi commit. Thanh tiến độ khi ấy đứng ở 0% rồi nhảy thẳng lên
100% — tức là đúng thứ FR-NFR-044 sinh ra để tránh.

Vì vậy: một `Session` thứ hai, commit từng lượt báo, chạy dưới vai trò
`ket_worker` (nó có `UPDATE` trên đúng bảng `jobs`). Cùng lý do và cùng chiều
với cờ hủy — cờ do một request HTTP khác đặt, nên phải **đọc lại từ DB** chứ
không đọc thuộc tính trên đối tượng job đã nạp từ đầu.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.jobs import queue
from ket.kernel.logging_setup import get_logger
from ket.kernel.persistence.session import worker_session
from ket.kernel.persistence.types import AuditValues

logger = get_logger(__name__)


class LeaseLostError(RuntimeError):
    """Job đã bị reaper thu hồi và (có thể) giao cho worker khác.

    `RuntimeError` chứ không `DomainError`: không có người dùng nào sửa được
    tình huống này và nó không đi ra API. Worker bắt nó, bỏ job đang chạy giữa
    chừng và đi tiếp — ghi tiếp kết quả lên một job đã thuộc về worker khác mới
    là điều tệ.
    """


class WorkerProgress:
    """Bản thi công của `JobProgress` cho tiến trình worker thật.

    Gộp ba việc vốn phải đi cùng nhau vào một lượt ghi: tiến độ, thông điệp, và
    **gia hạn lease**. Tách chúng ra là cách một job chạy đúng bị reaper cướp
    giữa chừng chỉ vì nhánh báo tiến độ quên gia hạn.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        job_id: UUID,
        dataset_schema: str,
        lease: timedelta,
        attempt: int,
    ) -> None:
        self._factory = factory
        self._job_id = job_id
        self._attempt = attempt
        self._schema = dataset_schema
        self._lease = lease

    def report(self, percent: int, message: str | None = None) -> None:
        """Ghi tiến độ và gia hạn lease trong một transaction ngắn.

        **Mỗi lượt gọi là một lượt ghi**, không tiết chế — đó là hợp đồng với
        thân job: `report` vừa là tiến độ vừa là nhịp gia hạn lease, nên bỏ qua
        một lượt gọi là bỏ qua một nhịp heartbeat, và một job đang chạy tốt bị
        reaper thu hồi. Trách nhiệm ngược lại thuộc thân job: gọi theo **lô
        việc** (mỗi vài giây), không gọi trong vòng lặp trong cùng.

        Từng có một `beat()` tiết chế theo nhịp ở đây; nó bị gỡ vì **không ai
        gọi** — một cơ chế chống-quá-tải không có người dùng chỉ làm người đọc
        tưởng heartbeat đã được lo.
        """
        self._write(progress=percent, message=message)

    def cancel_requested(self) -> bool:
        """Người dùng đã bấm Hủy chưa (đọc lại từ DB, xem docstring module)."""
        with worker_session(self._factory, dataset_schema=self._schema) as session:
            return queue.cancel_requested(session, self._job_id)

    def checkpoint(self, state: AuditValues) -> None:
        """Ghi mốc chạy tiếp — cũng bằng connection riêng, xem `JobProgress`."""
        with worker_session(self._factory, dataset_schema=self._schema) as session:
            alive = queue.checkpoint(session, self._job_id, attempt=self._attempt, state=state)
        if not alive:
            raise LeaseLostError(self._lost_message())

    def _write(self, *, progress: int | None, message: str | None) -> None:
        with worker_session(self._factory, dataset_schema=self._schema) as session:
            alive = queue.heartbeat(
                session,
                self._job_id,
                attempt=self._attempt,
                lease=self._lease,
                progress=progress,
                message=message,
            )
        if not alive:
            raise LeaseLostError(self._lost_message())

    def _lost_message(self) -> str:
        return (
            f"Tác vụ {self._job_id} (lượt {self._attempt}) không còn giữ lease — "
            "reaper đã thu hồi và có thể đã giao cho tiến trình khác. Dừng lại thay vì "
            "ghi đè việc của lượt chạy mới."
        )
