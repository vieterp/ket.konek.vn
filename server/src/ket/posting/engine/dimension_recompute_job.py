"""Job nền `posting.dimensions.recompute` — lát 6G-2 (M-9).

Job chứ không endpoint tính ngay, cùng lý do với recalc và integrity: rà lại
mọi chứng từ đã ghi sổ của một chi nhánh là việc nhiều giây trên dữ liệu thật,
và FR-NFR-042 cấm giữ request HTTP lâu như thế. Per-branch dưới RLS người xếp
hàng (`ACTING_BRANCH`, khuôn 4B): rà cả công ty = xếp job cho từng chi nhánh,
đúng như `posting.integrity.check`.

Hai chế độ trong MỘT job thay vì hai job: `apply=false` (mặc định) chỉ báo, và
đó cũng là dạng "phép kiểm toàn vẹn thứ chín" — chạy được cả trên kỳ đã khóa vì
không ghi gì. `apply=true` mới ghi đè. Mặc định là chế độ **không ghi**: một
job sửa sổ không được là thứ chạy nhầm chỉ vì quên một tham số.

Quyền: dùng lại `posting.integrity.*` — cùng nghiệp vụ (soát lại tính đúng của
sổ đã ghi), cùng người làm, và một mã quyền mới cho một việc không tách khỏi
việc cũ chỉ làm ma trận phân quyền dài thêm mà không cho ai lựa chọn thật.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.posting.engine.dimension_recompute import recompute_derived_dimensions
from ket.posting.integrity.job import INTEGRITY_RUN


class DimensionRecomputeParams(BaseModel):
    apply: bool = False
    """`false` = chỉ báo chỗ lệch; `true` = ghi đè chiều lệch trên dòng đã ghi
    sổ. Mặc định không ghi (xem docstring đầu tệp)."""


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
    drifts: list[DriftOut]


def run_dimension_recompute(context: JobContext, params: DimensionRecomputeParams) -> JobResult:
    if context.branch_id is None:
        raise JobParamsInvalidError(
            "Tác vụ tính lại chiều phải được xếp hàng từ một chi nhánh đang thao tác",
            job_type=DIMENSION_RECOMPUTE_JOB.code,
        )
    context.progress.report(percent=0, message="Đang rà chứng từ đã ghi sổ")
    outcome = recompute_derived_dimensions(
        context.session, branch_id=context.branch_id, apply=params.apply
    )
    context.progress.report(
        percent=100,
        message=(
            f"Rà {outcome.vouchers_scanned} chứng từ, lệch {len(outcome.drifts)} dòng, "
            f"sửa {outcome.applied}"
        ),
    )
    report = DimensionRecomputeReport(
        branch_id=context.branch_id,
        applied=outcome.applied,
        vouchers_scanned=outcome.vouchers_scanned,
        unresolved_vouchers=[str(voucher_id) for voucher_id in outcome.unresolved_vouchers],
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


DIMENSION_RECOMPUTE_JOB: Final[JobType[DimensionRecomputeParams]] = JobType(
    code="posting.dimensions.recompute",
    permission=INTEGRITY_RUN,
    # Chạy lại từ đầu luôn cho cùng kết quả: lượt trước đã sửa thì lượt sau
    # không còn gì lệch để sửa — phép ghi đè là idempotent theo đúng nghĩa.
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=DimensionRecomputeParams,
    handler=run_dimension_recompute,
    description="Tính lại chiều suy ra của dòng đã ghi sổ; mặc định chỉ báo, không sửa",
)

REGISTRY.register(DIMENSION_RECOMPUTE_JOB)
