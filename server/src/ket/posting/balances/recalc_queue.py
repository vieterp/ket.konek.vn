"""Đánh dấu kỳ bẩn — nửa "ghi" của hàng đợi tính lại (slice 4A).

`PostingService` gọi hàm này trong **chính** transaction ghi sổ: chứng từ vào
sổ mà dấu bẩn không vào theo (hoặc ngược lại) là snapshot nói dối có chữ ký.
Nửa "đọc" (job tính lại, dịch vụ truy vấn chọn snapshot-hay-tính-thẳng) thuộc
slice 4B.
"""

from __future__ import annotations

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
    """Ghi một dấu bẩn, nuốt trùng lặp.

    `ON CONFLICT DO NOTHING` chứ không kiểm-rồi-ghi: hai người ghi sổ cùng kỳ
    cùng lúc là chuyện mỗi sáng, và kiểm-rồi-ghi cho một trong hai người một
    `IntegrityError` vô cớ. Dòng dirty thô hơn (kỳ sớm hơn) đã có thì dòng mới
    cho kỳ muộn hơn vẫn được ghi — job tính lại (4B) tự gộp bằng cách lấy
    `MIN(from_period_id)` cho mỗi `(sổ, chi nhánh)`.
    """
    statement = (
        insert(BalanceRecalcQueue)
        .values(
            ledger=ledger,
            branch_id=branch_id,
            from_period_id=from_period_id,
            reason=reason,
        )
        .on_conflict_do_nothing(index_elements=["ledger", "branch_id", "from_period_id"])
    )
    session.execute(statement)
