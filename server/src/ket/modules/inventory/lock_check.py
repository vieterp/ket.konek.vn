"""Mục kiểm khóa sổ của kho (BR-STK-05, bước 11 phase 8): kỳ không khóa được khi
giá xuất kho của kỳ chưa chốt.

Hai điều kiện, cùng lý do với `PeriodLockService._ensure_recalc_queue_clear`
(số của kỳ đã khóa là số đã chốt):

* `inventory_recalc_queue` còn dấu có `from_date` ≤ ngày cuối kỳ **cùng niên
  độ** — lượt tính lại chạy từ ngày ấy tới hết năm nên sẽ viết lại giá vốn
  của chính kỳ này sau khi khóa;
* kỳ còn movement `cost_state <> COSTED` — giá vốn chưa có thì bút toán 632
  chưa có, và một kỳ khóa với doanh thu đã ghi mà giá vốn chưa ghi là vi phạm
  BR-SAL-01 ở dạng không sửa được.

Ở 8A chưa có engine tính giá, nên kỳ có phiếu xuất là chưa khóa được — đúng
nghĩa, và không chạm test khóa kỳ hiện có (chúng không lập phiếu kho). Chạy
dưới phạm vi mọi chi nhánh (`_ensure_scope_covers_every_branch` đã bắt trước).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.errors import PeriodLockChecklistError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.modules.inventory.models import CostState, InventoryMovement, InventoryRecalcMark

INVENTORY_COSTING_CHECK = "inventory_costed"


def ensure_inventory_costed(session: Session, period: AccountingPeriod, year: FiscalYear) -> None:
    pending_marks = session.scalar(
        select(func.count())
        .select_from(InventoryRecalcMark)
        .where(
            InventoryRecalcMark.from_date >= year.start_date,
            InventoryRecalcMark.from_date <= period.end_date,
        )
    )
    uncosted = session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.period_id == period.id,
            InventoryMovement.cost_state != CostState.COSTED,
        )
    )
    if not pending_marks and not uncosted:
        return
    raise PeriodLockChecklistError(
        "Giá xuất kho của kỳ chưa chốt — chạy tính giá xuất kho cho hết dấu bẩn và "
        "dòng chưa tính giá rồi mới khóa",
        check=INVENTORY_COSTING_CHECK,
        period_no=period.period_no,
        pending_marks=int(pending_marks or 0),
        uncosted_movements=int(uncosted or 0),
    )
