"""Tra/tạo lô theo `(mã hàng, số lô)` — dùng chung cho phiếu kho và tồn đầu kỳ.

Tách khỏi `service.py` ở 8C-1 vì cổng số dư ban đầu (`opening_port.py`) cũng
phải đổi `lot_no` của sheet thành `lot_id`, và hai chỗ tra lô bằng hai câu khác
nhau là hai chỗ để lệch nhau về luật "trim rồi so nguyên văn".
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ket.modules.inventory.models import Lot


def lot_id_for(session: Session, item_id: int, lot_no: str | None) -> int | None:
    """`None` khi không theo dõi lô; còn lại là id của lô, tạo nếu chưa có.

    Upsert chứ không kiểm-rồi-ghi: hai phiếu cùng lô mới lập song song là
    chuyện thường, và kiểm-rồi-ghi cho một trong hai `IntegrityError` vô cớ trên
    `uq_lots_item_lot_no` (review 8A L-2). `DO NOTHING` rồi đọc lại.
    """
    if lot_no is None or not lot_no.strip():
        return None
    code = lot_no.strip()
    session.execute(
        insert(Lot)
        .values(item_id=item_id, lot_no=code)
        .on_conflict_do_nothing(constraint="uq_lots_item_lot_no")
    )
    lot_id = session.scalar(select(Lot.id).where(Lot.item_id == item_id, Lot.lot_no == code))
    if lot_id is None:  # pragma: no cover - upsert vừa bảo đảm dòng tồn tại
        raise RuntimeError(f"Không tra được lô {code!r} của mã hàng {item_id}")
    return int(lot_id)
