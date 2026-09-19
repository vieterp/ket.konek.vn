"""Job nền `inventory.costing.recalc` — đường chạy chính của engine tính giá.

Khuôn `posting.balances.recalc_job`: per-branch (chi nhánh đang thao tác của
người bấm — phạm vi RLS thân job chạy dưới), advisory lock theo `(schema, chi
nhánh)` lấy TRƯỚC khi đọc dấu bẩn, toàn bộ lượt là MỘT transaction (hỏng hay
hủy giữa chừng → sổ kho, sổ cái, lớp tồn, snapshot và hàng đợi cùng quay về
nguyên trạng), dấu bẩn chỉ bị xóa đúng phiên bản đã đọc ở cuối.

Vì sao là job chứ không endpoint: 100.000 dòng là việc hàng phút (spike 8B),
và FR-NFR-042/044 cấm giữ một request HTTP lâu như thế. Xếp hàng qua
`POST /api/v1/jobs` với `type` này; xem trước FR-STK-003 ở
`GET /inventory/costing/affected` trước khi bấm.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import BaseModel
from sqlalchemy import text

from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import (
    REGISTRY,
    JobCancelled,
    JobContext,
    JobResult,
    JobType,
)
from ket.modules.inventory.costing import COSTING_RUN
from ket.modules.inventory.costing.engine import CostingCancelled, recalc_branch
from ket.modules.inventory.movements import costing_lock_tag


class CostingRecalcParams(BaseModel):
    """Rỗng = tính mọi dấu bẩn + dòng chưa tính của chi nhánh đang thao tác.

    `from_date` = **ép** tính lại mọi khóa của chi nhánh từ ngày đó (chỉ năm
    tài chính chứa ngày ấy) — đường vá sau khôi phục sao lưu hay sửa dữ liệu
    ngoài posting engine, cùng vai `ledger + from_period_id` của job số dư.
    """

    from_date: date | None = None


def run_costing_recalc(context: JobContext, params: CostingRecalcParams) -> JobResult:
    if context.branch_id is None:
        raise JobParamsInvalidError(
            "Tác vụ tính giá xuất kho phải được xếp hàng từ một chi nhánh đang thao tác",
            job_type=COSTING_JOB.code,
        )
    branch_id = context.branch_id
    session = context.session
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tag)::bigint)"),
        {"tag": costing_lock_tag(context.dataset_schema, branch_id)},
    )
    try:
        result = recalc_branch(
            session,
            branch_id=branch_id,
            user_id=context.requested_by,
            force_from=params.from_date,
            progress=context.progress.report,
            cancel_requested=context.progress.cancel_requested,
        )
    except CostingCancelled as exc:
        raise JobCancelled(str(exc)) from exc
    return result.as_job_result()


COSTING_JOB: Final[JobType[CostingRecalcParams]] = JobType(
    code="inventory.costing.recalc",
    permission=COSTING_RUN,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=CostingRecalcParams,
    handler=run_costing_recalc,
    description="Tính giá xuất kho cho chi nhánh: tính lại theo dấu bẩn, ghi lại giá vốn",
)

REGISTRY.register(COSTING_JOB)
