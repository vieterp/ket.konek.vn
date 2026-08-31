"""Job nền `posting.dimensions.recompute` — lát 6G-2 (M-9).

Job chứ không endpoint tính ngay, cùng lý do với recalc và integrity: rà lại
mọi chứng từ đã ghi sổ của một chi nhánh là việc nhiều giây trên dữ liệu thật,
và FR-NFR-042 cấm giữ request HTTP lâu như thế. Per-branch dưới RLS người xếp
hàng (`ACTING_BRANCH`, khuôn 4B): rà cả công ty = xếp job cho từng chi nhánh,
đúng như `posting.integrity.check`.

**HAI loại job, không một loại với tham số `apply`** (review 6G-2 H-1):

* `posting.dimensions.recompute` — chỉ BÁO, quyền `posting.integrity.create`.
  Đây cũng là dạng "phép kiểm toàn vẹn thứ chín", chạy được trên kỳ đã khóa vì
  không ghi gì.
* `posting.dimensions.apply` — GHI lại chiều lệch, quyền
  `posting.integrity.edit` (mã mới, xem `posting/integrity/__init__.py`).

Vì sao không phải một job với cờ: quyền được kiểm theo **loại job** lúc xếp
hàng. Gói cả hai vào một loại nghĩa là mã quyền chỉ-đọc `posting.integrity.*`
bỗng trở thành quyền ghi vào `gl_postings`, và người quản trị đọc ma trận phân
quyền không có cách nào thấy điều đó. Hai loại cũng xóa hẳn ca "chạy nhầm vì
quên một tham số".

Cả hai per-branch dưới RLS người xếp hàng (`ACTING_BRANCH`, khuôn 4B): rà cả
công ty = xếp job cho từng chi nhánh, đúng như `posting.integrity.check`.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.posting.engine.dimension_recompute import recompute_derived_dimensions
from ket.posting.integrity.job import INTEGRITY_EDIT, INTEGRITY_RUN


class DimensionRecomputeParams(BaseModel):
    """Không tham số: chế độ nằm ở LOẠI job, không ở thân yêu cầu."""


class DriftOut(BaseModel):
    voucher_id: str
    voucher_no: str
    ledger: int
    line_no: int
    dimension: str
    stored: int | None
    expected: int | None


class DimensionRecomputeReport(BaseModel):
    """Thân kết quả job — hình dạng này là hợp đồng đọc của UI/nhật ký."""

    branch_id: int
    applied: int
    vouchers_scanned: int
    unresolved_vouchers: list[str]
    locked_vouchers: list[str]
    """Chứng từ có lệch nhưng ở kỳ đã khóa: BÁO, không sửa (H-1)."""

    drifts: list[DriftOut]


def _run(context: JobContext, *, apply: bool, job_code: str) -> JobResult:
    if context.branch_id is None:
        raise JobParamsInvalidError(
            "Tác vụ tính lại chiều phải được xếp hàng từ một chi nhánh đang thao tác",
            job_type=job_code,
        )
    context.progress.report(percent=0, message="Đang rà chứng từ đã ghi sổ")
    outcome = recompute_derived_dimensions(
        context.session, branch_id=context.branch_id, apply=apply
    )
    context.progress.report(
        percent=100,
        message=(
            f"Rà {outcome.vouchers_scanned} chứng từ, lệch {len(outcome.drifts)} dòng, "
            f"sửa {outcome.applied}, bỏ qua {len(outcome.locked_vouchers)} chứng từ kỳ khóa"
        ),
    )
    report = DimensionRecomputeReport(
        branch_id=context.branch_id,
        applied=outcome.applied,
        vouchers_scanned=outcome.vouchers_scanned,
        unresolved_vouchers=[str(voucher_id) for voucher_id in outcome.unresolved_vouchers],
        locked_vouchers=[str(voucher_id) for voucher_id in outcome.locked_vouchers],
        drifts=[
            DriftOut(
                voucher_id=str(drift.voucher_id),
                voucher_no=drift.voucher_no,
                ledger=drift.ledger,
                line_no=drift.line_no,
                dimension=drift.dimension,
                stored=drift.stored,
                expected=drift.expected,
            )
            for drift in outcome.drifts
        ],
    )
    # Cùng lối `posting.integrity.check`: thân kết quả job là JSON lồng, dựng
    # qua model rồi `model_dump(mode="json")` để không có dict tay nào trôi.
    return dict(report.model_dump(mode="json"))


def run_dimension_report(context: JobContext, params: DimensionRecomputeParams) -> JobResult:
    return _run(context, apply=False, job_code=DIMENSION_RECOMPUTE_JOB.code)


def run_dimension_apply(context: JobContext, params: DimensionRecomputeParams) -> JobResult:
    return _run(context, apply=True, job_code=DIMENSION_APPLY_JOB.code)


DIMENSION_RECOMPUTE_JOB: Final[JobType[DimensionRecomputeParams]] = JobType(
    code="posting.dimensions.recompute",
    permission=INTEGRITY_RUN,
    # Chạy lại từ đầu luôn cho cùng kết quả: nó chỉ đọc.
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=DimensionRecomputeParams,
    handler=run_dimension_report,
    description="Rà chiều suy ra của dòng đã ghi sổ và BÁO chỗ lệch; không sửa gì",
)

DIMENSION_APPLY_JOB: Final[JobType[DimensionRecomputeParams]] = JobType(
    code="posting.dimensions.apply",
    permission=INTEGRITY_EDIT,
    # Lượt trước đã sửa thì lượt sau không còn gì lệch để sửa — phép ghi đè là
    # idempotent theo đúng nghĩa.
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=DimensionRecomputeParams,
    handler=run_dimension_apply,
    description="Ghi lại chiều suy ra đã lệch (bỏ qua chứng từ ở kỳ đã khóa)",
)

REGISTRY.register(DIMENSION_RECOMPUTE_JOB)
REGISTRY.register(DIMENSION_APPLY_JOB)
