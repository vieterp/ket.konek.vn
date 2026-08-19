"""Job nền `posting.balances.recalc` — đường chạy chính của tính lại snapshot.

Vì sao là job chứ không phải một endpoint tính ngay: tính lại một năm dày
chứng từ là việc chục giây (mốc spike S5: < 60s/năm trên 100k dòng), và
FR-NFR-042/044 cấm giữ một request HTTP lâu như thế. Xếp hàng qua
`POST /api/v1/jobs` với `type` này; chi nhánh của job = chi nhánh đang thao tác
của người bấm (xem `routers/jobs.py`) — đó chính là quyết định "recalc chạy
per-branch theo phạm vi RLS" của bàn giao 4A→4B.

Toàn bộ job là **một** transaction (hợp đồng `JobContext`: thân job không tự
commit). Lệch có chủ đích so với phase file ("mỗi kỳ 1 transaction"): khuôn job
của kernel mở sẵn một `unit_of_work`, và một transaction cho cả lượt còn cho
bất biến mạnh hơn — dấu bẩn chỉ bị xóa khi MỌI kỳ đã tính xong; hỏng giữa
chừng thì snapshot lẫn hàng đợi cùng quay về nguyên trạng, không có trạng thái
"tính nửa năm, hàng đợi đã trống". Hủy giữa chừng cũng vậy (`JobCancelled`
rollback cả khối).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field
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
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.balances import recalc
from ket.posting.engine.models import Ledger

BALANCE_PERMISSION_MODULE: Final[str] = "posting"
BALANCE_PERMISSION_CODE: Final[str] = "balance"

RECALC_RUN: Final[str] = permission_code(
    BALANCE_PERMISSION_MODULE, BALANCE_PERMISSION_CODE, Action.CREATE
)
BALANCE_VIEW: Final[str] = permission_code(
    BALANCE_PERMISSION_MODULE, BALANCE_PERMISSION_CODE, Action.VIEW
)


class BalanceRecalcParams(BaseModel):
    """Tham số job tính lại — mặc định rỗng là đúng cho gần như mọi lượt chạy.

    Không tham số → tính mọi dấu bẩn của chi nhánh đang thao tác. Cặp
    `ledger` + `from_period_id` là đường **ép** tính lại từ một kỳ cụ thể kể cả
    khi hàng đợi trống — dùng sau khi sửa dữ liệu bằng đường không đi qua
    posting engine (khôi phục sao lưu, vá tay có kiểm soát).
    """

    ledger: int | None = Field(default=None, ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)
    from_period_id: int | None = Field(default=None, ge=1)


def run_balance_recalc(context: JobContext, params: BalanceRecalcParams) -> JobResult:
    """Đọc dấu bẩn của chi nhánh, tính lại từng dải kỳ, xóa đúng dấu đã đọc."""
    if (params.ledger is None) != (params.from_period_id is None):
        raise JobParamsInvalidError(
            "Ép tính lại phải nêu đủ cả sổ lẫn kỳ bắt đầu",
            job_type=RECALC_JOB.code,
            field="ledger" if params.ledger is None else "from_period_id",
        )
    if context.branch_id is None:
        # Job không mang chi nhánh chạy với phạm vi RLS rỗng — không thấy dòng
        # nào để tính. Từ chối sớm với thông điệp chỉ đúng cách dùng.
        raise JobParamsInvalidError(
            "Tác vụ tính lại số dư phải được xếp hàng từ một chi nhánh đang thao tác",
            job_type=RECALC_JOB.code,
        )
    branch_id = context.branch_id
    session = context.session

    # Một chi nhánh — một lượt tính tại một thời điểm. Hai job song song không
    # hỏng dữ liệu (transaction thua rollback trọn) nhưng job thua chết bằng
    # `duplicate key uq_account_balances_key` thô — "hai người cùng bấm tính
    # lại sau import lớn" xứng đáng được xếp hàng chứ không phải một job FAILED.
    # Khóa advisory theo (schema, chi nhánh) vì advisory lock là phạm vi cả
    # database — thiếu schema thì hai dataset trùng `branch_id` chặn nhau oan.
    # Lấy khóa TRƯỚC khi đọc dấu bẩn: job chờ xong mới đọc, nên nó thấy hàng
    # đợi như job trước để lại chứ không phải bản chụp cũ.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tag)::bigint)"),
        {"tag": f"posting.balances.recalc:{context.dataset_schema}:{branch_id}"},
    )

    marks = recalc.pending_marks(session, branch_id=branch_id)
    forced = (
        ((params.ledger, params.from_period_id),)
        if params.ledger is not None and params.from_period_id is not None
        else ()
    )
    runs = recalc.build_runs(session, marks, forced=forced)

    total = sum(len(run.periods) for run in runs)
    if total == 0:
        context.progress.report(percent=100, message="Không có kỳ nào chờ tính lại")
        return {"periods_recalced": 0, "marks_cleared": 0, "branch_id": branch_id}

    done = 0
    for run in runs:
        for period in run.periods:
            # Ranh giới lô = một kỳ: hủy có hiệu lực giữa hai kỳ, không bao giờ
            # giữa chừng một câu SQL — và `JobCancelled` rollback cả transaction
            # nên không có snapshot nửa vời nào được commit.
            if context.progress.cancel_requested():
                raise JobCancelled(f"Người dùng hủy sau {done}/{total} kỳ")
            recalc.recalc_period(session, ledger=run.ledger, branch_id=branch_id, period=period)
            done += 1
            context.progress.report(
                percent=done * 100 // total,
                message=f"Đã tính lại {done}/{total} kỳ",
            )

    cleared = recalc.clear_marks(session, branch_id=branch_id, marks=marks)
    return {"periods_recalced": done, "marks_cleared": cleared, "branch_id": branch_id}


RECALC_JOB: Final[JobType[BalanceRecalcParams]] = JobType(
    code="posting.balances.recalc",
    permission=RECALC_RUN,
    # Chạy lại từ đầu luôn an toàn: mỗi kỳ là DELETE + INSERT trọn lát, và dấu
    # bẩn chỉ bị xóa trong cùng transaction với kỳ cuối — lượt dở dang rollback
    # sạch, lượt sau thấy nguyên hàng đợi.
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=BalanceRecalcParams,
    handler=run_balance_recalc,
    description="Tính lại snapshot số dư tài khoản của chi nhánh từ sổ cái",
)

REGISTRY.register(RECALC_JOB)
