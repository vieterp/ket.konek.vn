"""Đánh dấu kỳ bẩn — nửa "ghi" của hàng đợi tính lại.

`PostingService` gọi hàm này trong **chính** transaction ghi sổ: chứng từ vào
sổ mà dấu bẩn không vào theo (hoặc ngược lại) là snapshot nói dối có chữ ký.
Nửa "đọc" nằm ở `recalc.py` (job tính lại) và `query_service.py` (chọn
snapshot-hay-tính-thẳng).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ket.posting.balances.models import BalanceRecalcQueue


def mark_dirty(
    session: Session,
    *,
    ledger: int,
    branch_id: int,
    from_period_id: int,
    reason: str,
) -> None:
    """Ghi một dấu bẩn; dấu đã có thì **làm mới `marked_at`** thay vì bỏ qua.

    Upsert chứ không kiểm-rồi-ghi: hai người ghi sổ cùng kỳ cùng lúc là chuyện
    mỗi sáng, và kiểm-rồi-ghi cho một trong hai người một `IntegrityError` vô
    cớ. Dòng dirty thô hơn (kỳ sớm hơn) đã có thì dòng mới cho kỳ muộn hơn vẫn
    được ghi — job tính lại tự gộp bằng cách lấy `MIN(from_period_id)` cho mỗi
    `(sổ, chi nhánh)`.

    Vì sao `DO UPDATE SET marked_at` chứ không `DO NOTHING` (đổi ở 4B): job
    tính lại chỉ được xóa đúng **phiên bản dấu bẩn nó đã đọc**. Một lượt ghi sổ
    chen vào giữa lúc job đang tính sẽ làm mới `marked_at`; câu DELETE của job
    so `marked_at` bằng giá trị đã đọc nên dấu vừa làm mới **sống sót**, và kỳ
    đó được tính lại ở lượt sau thay vì bị coi là sạch oan. `DO NOTHING` để
    nguyên `marked_at` cũ thì chính lượt ghi chen ngang đó bị xóa mất dấu.

    `clock_timestamp()` chứ không `now()`: `now()` đóng băng tại đầu
    transaction, nên hai lượt ghi sổ trong cùng một transaction dài (import
    lớn) sẽ cho cùng một `marked_at` — phép so-bằng của job mất độ phân giải.
    """
    statement = (
        insert(BalanceRecalcQueue)
        .values(
            ledger=ledger,
            branch_id=branch_id,
            from_period_id=from_period_id,
            marked_at=text("clock_timestamp()"),
            reason=reason,
        )
        .on_conflict_do_update(
            index_elements=["ledger", "branch_id", "from_period_id"],
            set_={"marked_at": text("clock_timestamp()"), "reason": reason},
        )
    )
    session.execute(statement)
