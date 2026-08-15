"""Loại job nền tảng của phase 2 — ba loại, mỗi loại chứng minh một hình dạng.

Không phải bộ sưu tập tiện ích: mỗi loại ở đây tồn tại để một cơ chế của hàng
đợi có ít nhất một người dùng thật, thay vì chỉ có test.

| Loại | Chứng minh |
| --- | --- |
| `system.diagnostic.slow_task` | Tiến độ %, nút Hủy, và tiêu chí FR-NFR-042 (job 30 giây mà API vẫn phản hồi < 200ms) |
| `system.maintenance.prune_idempotency_keys` | Job **theo dataset** chạy dưới đúng vai trò runtime của dataset đó |
| `system.maintenance.prune_sessions` | Job cần kết nối đặc quyền — và cách từ chối nó khi bản cài chưa cấu hình (xem `JobPrivilege.CONTROL_OWNER`) |

Cả ba đều còn **lệnh CLI tương ứng** (`python -m ket.admin`). Không phải trùng
lặp: CLI là đường phá-kính chạy được khi hàng đợi hoặc worker đang hỏng, đúng
lúc người ta cần nó nhất.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Final

from pydantic import BaseModel, Field

from ket.kernel.idempotency import service as idempotency_service
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import (
    REGISTRY,
    JobCancelled,
    JobContext,
    JobPrivilege,
    JobResult,
    JobType,
)
from ket.kernel.security import auth_service
from ket.kernel.security.auth_service import DEFAULT_SESSION_RETENTION
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code

MAINTENANCE_DOCUMENT: Final[str] = "maintenance"
INSTALLATION_DOCUMENT: Final[str] = "installation"
JOB_DOCUMENT: Final[str] = "job"

JOB_VIEW: Final[str] = permission_code(SYSTEM_MODULE, JOB_DOCUMENT, Action.VIEW)
JOB_CREATE: Final[str] = permission_code(SYSTEM_MODULE, JOB_DOCUMENT, Action.CREATE)
JOB_CANCEL: Final[str] = permission_code(SYSTEM_MODULE, JOB_DOCUMENT, Action.EDIT)
MAINTENANCE_RUN: Final[str] = permission_code(SYSTEM_MODULE, MAINTENANCE_DOCUMENT, Action.CREATE)
INSTALLATION_RUN: Final[str] = permission_code(SYSTEM_MODULE, INSTALLATION_DOCUMENT, Action.CREATE)
"""Quyền chạy tác vụ chạm **bảng dùng chung của cả bản cài**.

Riêng khỏi `MAINTENANCE_RUN` vì quyền được cấp trong phạm vi **một** dữ liệu kế
toán: dùng chung một mã cho cả hai loại nghĩa là người quản trị doanh nghiệp B
xóa được lịch sử đăng nhập của mọi người, kể cả người chưa từng mở B."""

SLOW_TASK_STEP: Final[timedelta] = timedelta(milliseconds=500)
"""Độ dài một lô của job chẩn đoán = độ trễ tối đa của nút Hủy.

Hủy chỉ có hiệu lực ở **ranh giới lô** (job thật không được giết giữa một bút
toán), nên độ dài lô chính là lời hứa với người bấm nút. Nửa giây đủ ngắn để
cảm giác là "dừng ngay" mà không biến vòng lặp thành một trận bão truy vấn kiểm
cờ hủy.

`timedelta` chứ không một số giây dấu phẩy động: bộ quét của
`tests/test_no_float_in_domain.py` cấm **tên `float` và literal dấu phẩy động**
trong `src/ket` (ADR-015). `timedelta` giữ đúng tinh thần đó — không có hằng số
dấu phẩy động nào trong mã nghiệp vụ; `total_seconds()` chỉ xuất hiện ở đúng
lời gọi `time.sleep`, nơi API của thư viện chuẩn đòi giây."""

SLOW_TASK_REPORT_EVERY: Final[timedelta] = timedelta(seconds=2)
"""Khoảng cách giữa hai lượt báo tiến độ. Xem `run_slow_task`."""


class SlowTaskParams(BaseModel):
    """Tham số job chẩn đoán."""

    seconds: int = Field(default=30, ge=1, le=600)
    """Chạy bao lâu. Mặc định 30 giây = đúng kịch bản đo của FR-NFR-042."""


class PruneIdempotencyKeysParams(BaseModel):
    """Không có tham số — thời hạn đã nằm trong chính cột `expires_at` của khóa."""


class PruneSessionsParams(BaseModel):
    """Tham số job dọn phiên đăng nhập."""

    retention_days: int = Field(default=DEFAULT_SESSION_RETENTION.days, ge=7, le=3650)
    """Giữ phiên **đã kết thúc** bao nhiêu ngày.

    Sàn **7 ngày**, không phải 0: `0` cắt tại thời điểm hiện tại, tức là một
    thao tác "dọn dẹp" xóa sạch mọi phiên vừa kết thúc — đúng thứ ai đó muốn làm
    để xóa dấu vết của chính mình. Một tuần là khoảng tối thiểu để còn điều tra
    được một sự cố cuối tuần. Đường CLI (`ket.admin prune-sessions`) giữ sàn
    riêng của nó vì nó chỉ chạy được bởi người đã chạm được máy chủ."""


def run_slow_task(context: JobContext, params: SlowTaskParams) -> JobResult:
    """Job chạy dài, có tiến độ và dừng được — dùng để đo và để diễn tập.

    Cố ý **không** chạm DB trong thân vòng lặp: nó đo độ phản hồi của API trong
    lúc worker bận, nên nó không được tự tạo tải DB làm nhiễu chính phép đo đó.

    `time.sleep` chứ không vòng lặp bận: worker là tiến trình riêng nên nó ngủ
    thật, không giữ GIL, không ăn CPU của tiến trình API (ADR-014).
    """
    step_ms = SLOW_TASK_STEP // timedelta(milliseconds=1)
    steps = max(1, params.seconds * 1000 // step_ms)
    # Báo tiến độ theo **lô**, không theo mỗi bước ngủ: `report` là một lượt ghi
    # DB và cũng là nhịp gia hạn lease (xem `JobProgress.report`), nên gọi nó
    # mỗi 500ms là hai lượt ghi mỗi giây lên cùng một dòng — không cần thiết cho
    # một thanh tiến độ mà mắt người đọc được.
    steps_per_report = max(1, int(SLOW_TASK_REPORT_EVERY / SLOW_TASK_STEP))
    for step in range(steps):
        if context.progress.cancel_requested():
            raise JobCancelled(f"Người dùng hủy sau {step} lô")
        time.sleep(SLOW_TASK_STEP.total_seconds())
        if (step + 1) % steps_per_report == 0 or step + 1 == steps:
            context.progress.report(
                percent=(step + 1) * 100 // steps,
                message=f"Đang chạy tác vụ chẩn đoán ({step + 1}/{steps})",
            )
    return {"slept_seconds": params.seconds}


def run_prune_idempotency_keys(
    context: JobContext, _params: PruneIdempotencyKeysParams
) -> JobResult:
    """Dọn khóa idempotency hết hạn của **dataset đang chạy job**.

    Chạy dưới vai trò dataset: `ds_<mã>_app` đã có `DELETE` trên bảng nghiệp vụ,
    nên job này không cần đặc quyền nào. Đây là hình dạng mà gần như mọi job của
    phase 4 trở đi sẽ có.
    """
    removed = idempotency_service.prune_expired_keys(context.session)
    context.progress.report(percent=100, message=f"Đã xóa {removed} khóa hết hạn")
    return {"removed": removed, "dataset_schema": context.dataset_schema}


def run_prune_sessions(context: JobContext, params: PruneSessionsParams) -> JobResult:
    """Dọn phiên đăng nhập đã kết thúc — **schema điều khiển, kết nối owner**.

    Vì sao không chạy được dưới vai trò runtime: `DELETE` trên `auth_sessions`
    bị thu hồi khỏi `ket_app` có chủ đích (lát 2B-1a), để một lỗ hổng ở tầng API
    không xóa được dấu vết ai đã đăng nhập lúc nào. Job này vì thế đòi
    `JobPrivilege.CONTROL_OWNER`, và worker chỉ chạy nó khi bản cài **tường
    minh** cấu hình một DSN `ket_owner` riêng cho worker (`worker_owner_database_url`).

    Trên bản cài mặc định, DSN đó để trống → job hỏng với thông điệp chỉ hai
    đường đi tiếp, và worker không cầm quyền owner. Đó là đánh đổi có chủ ý: đưa
    việc dọn vào hàng đợi là tiện, còn để mọi worker cầm quyền owner **mặc
    định** thì tiện đó mua bằng chính bất biến mà `auth_sessions` dựng ra.

    `prune_expired_sessions` ghi `control_audit_log` trong cùng transaction, nên
    dấu vết "ai dọn, lúc nào, bao nhiêu dòng" không mất theo dữ liệu bị dọn.
    """
    removed = auth_service.prune_expired_sessions(
        context.session,
        retention=timedelta(days=params.retention_days),
        actor_user_id=context.requested_by,
        client_info=f"ket.worker job={context.job_id}",
    )
    context.progress.report(percent=100, message=f"Đã xóa {removed} phiên đã kết thúc")
    return {"removed": removed, "retention_days": params.retention_days}


SLOW_TASK: Final[JobType[SlowTaskParams]] = JobType(
    code="system.diagnostic.slow_task",
    permission=JOB_CREATE,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=SlowTaskParams,
    handler=run_slow_task,
    description="Tác vụ chẩn đoán chạy dài — đo độ phản hồi API và diễn tập nút Hủy",
)

PRUNE_IDEMPOTENCY_KEYS: Final[JobType[PruneIdempotencyKeysParams]] = JobType(
    code="system.maintenance.prune_idempotency_keys",
    permission=MAINTENANCE_RUN,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=PruneIdempotencyKeysParams,
    handler=run_prune_idempotency_keys,
    description="Xóa khóa idempotency đã hết hạn của dữ liệu kế toán này",
)

PRUNE_SESSIONS: Final[JobType[PruneSessionsParams]] = JobType(
    code="system.maintenance.prune_sessions",
    permission=INSTALLATION_RUN,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=PruneSessionsParams,
    handler=run_prune_sessions,
    privilege=JobPrivilege.CONTROL_OWNER,
    description="Xóa phiên đăng nhập đã kết thúc và đủ cũ (cần kết nối đặc quyền)",
)

for _job_type in (SLOW_TASK, PRUNE_IDEMPOTENCY_KEYS, PRUNE_SESSIONS):
    REGISTRY.register(_job_type)
