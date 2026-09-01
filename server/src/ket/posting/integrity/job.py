"""Job nền `posting.integrity.check` — chạy bộ kiểm toàn vẹn theo yêu cầu.

Job chứ không phải endpoint tính ngay, cùng lý do với recalc: trên dữ liệu
100k chứng từ/năm các câu đối chiếu là việc nhiều giây, và FR-NFR-042 cấm giữ
request HTTP lâu như thế. Chạy dưới phạm vi RLS của người xếp hàng
(per-branch, như recalc 4B): báo cáo chênh lệch chỉ nói về chi nhánh mà người
yêu cầu được thấy — muốn kiểm toàn dữ liệu thì người có phạm vi đủ rộng xếp
job cho từng chi nhánh.

Chỉ đọc — không sửa, không khóa gì: hai job integrity chạy song song hay chạy
đè lên một lượt ghi sổ đều vô hại (kết quả phản ánh snapshot transaction của
nó). `IDEMPOTENT_RESTART` vì chạy lại từ đầu luôn cho cùng một loại kết quả.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobCancelled, JobContext, JobResult, JobType
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.integrity.runner import (
    CheckOutcome,
    IntegrityReport,
    resolve_checks,
    run_check,
)

INTEGRITY_PERMISSION_MODULE: Final[str] = "posting"
INTEGRITY_PERMISSION_CODE: Final[str] = "integrity"

INTEGRITY_RUN: Final[str] = permission_code(
    INTEGRITY_PERMISSION_MODULE, INTEGRITY_PERMISSION_CODE, Action.CREATE
)
INTEGRITY_EDIT: Final[str] = permission_code(
    INTEGRITY_PERMISSION_MODULE, INTEGRITY_PERMISSION_CODE, Action.EDIT
)
"""Quyền GHI của nhóm soát sổ — hiện chỉ job `posting.dimensions.apply` dùng
(review 6G-2 H-1). Tách khỏi `INTEGRITY_RUN` để vai trò chỉ soát sổ không kèm
quyền ghi vào `gl_postings`."""

INTEGRITY_VIEW: Final[str] = permission_code(
    INTEGRITY_PERMISSION_MODULE, INTEGRITY_PERMISSION_CODE, Action.VIEW
)


class IntegrityCheckParams(BaseModel):
    """Không tham số = chạy đủ bộ trong `CHECKS`; `checks` thu hẹp khi cần lặp
    nhanh một phép vừa sửa xong dữ liệu."""

    checks: list[str] | None = None


def run_integrity_check(context: JobContext, params: IntegrityCheckParams) -> JobResult:
    if context.branch_id is None:
        raise JobParamsInvalidError(
            "Tác vụ kiểm tra toàn vẹn phải được xếp hàng từ một chi nhánh đang thao tác",
            job_type=INTEGRITY_JOB.code,
        )
    checks = resolve_checks(params.checks)

    outcomes: list[CheckOutcome] = []
    for index, check in enumerate(checks, start=1):
        if context.progress.cancel_requested():
            raise JobCancelled(f"Người dùng hủy sau {index - 1}/{len(checks)} phép kiểm")
        outcomes.append(run_check(context.session, check, branch_id=context.branch_id))
        context.progress.report(
            percent=index * 100 // len(checks),
            message=f"Đã kiểm {index}/{len(checks)}: {check.title}",
        )

    report = IntegrityReport(branch_id=context.branch_id, checks=outcomes)
    payload = report.model_dump(mode="json")
    payload["total_discrepancies"] = report.total_discrepancies
    return dict(payload)


INTEGRITY_JOB: Final[JobType[IntegrityCheckParams]] = JobType(
    code="posting.integrity.check",
    permission=INTEGRITY_RUN,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=IntegrityCheckParams,
    handler=run_integrity_check,
    description="Đối chiếu sổ cái, snapshot và số dư ban đầu; báo chênh lệch, không tự sửa",
)

REGISTRY.register(INTEGRITY_JOB)
