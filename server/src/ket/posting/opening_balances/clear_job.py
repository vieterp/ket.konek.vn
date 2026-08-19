"""Job `posting.opening_balances.clear_group` — đưa một nhóm số dư về 0 (M5, 4E).

Ngữ nghĩa nhập từ tệp giữ nguyên an toàn: sheet vắng/rỗng = không đụng tới
(review 4C). Muốn xóa trọn một nhóm của (năm, sổ, chi nhánh) thì hành động
này là đường duy nhất — tường minh, có xác nhận ở UI, có nhật ký kèm số dòng
đã xóa.

Là job chứ không endpoint đồng bộ vì cùng lý do với import: đường ghi giữ
`FOR SHARE` trên hàng kỳ (RT-09) + advisory lock theo (năm, chi nhánh) — những
khóa không được sống trong một request HTTP treo theo mạng client. Cùng khuôn
xếp hàng với carry-forward: `POST /api/v1/jobs` với `direct_enqueue`.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.errors import DomainError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.kernel.periods.models import FiscalYear
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.guards import (
    acquire_opening_write_lock,
    guard_opening_writable,
)
from ket.posting.opening_balances.import_job import OPENING_WRITE
from ket.posting.opening_balances.models import OpeningBalance, OpeningDetailKind

CLEAR_GROUP_CODE: Final[str] = "posting.opening_balances.clear_group"


class ClearGroupParams(BaseModel):
    """Xóa trọn một nhóm của (năm, sổ) — chi nhánh lấy từ ngữ cảnh xếp hàng."""

    fiscal_year_id: int = Field(ge=1)
    ledger: int = Field(default=Ledger.FINANCIAL, ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)
    detail_kind: int

    @field_validator("detail_kind")
    @classmethod
    def _kind_owned_by_phase_4(cls, value: int) -> int:
        # Nhóm 5–9 (tồn kho, dở dang, CCDC, TSCĐ, CPTT) thuộc phase 8 cùng
        # bảng chi tiết của chúng — xóa mà không đụng bảng chi tiết là để lại
        # dữ liệu mồ côi, nên chặn từ tham số.
        if value not in OpeningDetailKind.PHASE_4_KINDS:
            raise ValueError(f"Nhóm số dư {value} chưa hỗ trợ xóa — chỉ nhóm 0–4")
        return value


def run_clear_group(context: JobContext, params: ClearGroupParams) -> JobResult:
    """Xóa mọi dòng của nhóm trong (năm, sổ, chi nhánh đang thao tác)."""
    if context.branch_id is None:
        raise DomainError("Lượt xóa số dư phải được xếp hàng từ một chi nhánh đang thao tác")
    branch_id = context.branch_id
    session: Session = context.session

    fiscal_year = session.get(FiscalYear, params.fiscal_year_id)
    if fiscal_year is None:
        raise DomainError("Năm tài chính không tồn tại", fiscal_year_id=params.fiscal_year_id)

    # Cùng thứ tự khóa với mọi đường ghi số dư (advisory trước, FOR SHARE kỳ
    # sau) — không deadlock với import/carry-forward đang chạy song song.
    acquire_opening_write_lock(
        session,
        dataset_schema=context.dataset_schema,
        branch_id=branch_id,
        fiscal_year_id=fiscal_year.id,
    )
    first_period = guard_opening_writable(session, fiscal_year, for_share=True)

    scope = [
        OpeningBalance.fiscal_year_id == fiscal_year.id,
        OpeningBalance.ledger == params.ledger,
        OpeningBalance.branch_id == branch_id,
        OpeningBalance.detail_kind == params.detail_kind,
    ]
    # Đếm trước rồi xóa: `rowcount` của DELETE đáng tin nhưng con số này còn
    # vào nhật ký — một truy vấn đếm rẻ đổi lấy audit không phụ thuộc driver.
    deleted_rows = int(
        session.execute(select(func.count()).select_from(OpeningBalance).where(*scope)).scalar_one()
    )
    if deleted_rows:
        # `opening_balance_invoices` đi theo cha qua ON DELETE CASCADE.
        session.execute(delete(OpeningBalance).where(*scope))
        # Bàn giao 4B: mọi đường ghi `opening_balances` đánh dấu bẩn kỳ đầu năm.
        mark_dirty(
            session,
            ledger=params.ledger,
            branch_id=branch_id,
            from_period_id=first_period.id,
            reason=f"opening_balances clear_group kind={params.detail_kind}",
        )

    record_action(
        session,
        entity_type="opening_balances",
        entity_id=str(context.job_id),
        action=AuditAction.DELETED,
        branch_id=branch_id,
        new_values={
            "fiscal_year_id": fiscal_year.id,
            "ledger": params.ledger,
            "detail_kind": params.detail_kind,
            "deleted_rows": deleted_rows,
        },
    )
    context.progress.report(100, f"Đã xóa {deleted_rows} dòng số dư nhóm {params.detail_kind}")
    return {
        "fiscal_year_id": fiscal_year.id,
        "ledger": params.ledger,
        "detail_kind": params.detail_kind,
        "deleted_rows": deleted_rows,
    }


CLEAR_GROUP_JOB: Final[JobType[ClearGroupParams]] = JobType(
    code=CLEAR_GROUP_CODE,
    permission=OPENING_WRITE,
    # Xóa-theo-điều-kiện trong một transaction: chạy lại từ đầu cho cùng kết
    # quả (lần hai xóa 0 dòng); lượt dở dang rollback sạch.
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=ClearGroupParams,
    handler=run_clear_group,
    description="Xóa trọn một nhóm số dư ban đầu của (năm, sổ, chi nhánh)",
)

REGISTRY.register(CLEAR_GROUP_JOB)
