"""Nửa "đọc" của `inventory_recalc_queue` — dấu bẩn như job đã thấy, và xóa
đúng phiên bản ấy (cùng khuôn `posting.balances.recalc.DirtyMark`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from ket.modules.inventory.models import InventoryRecalcMark


@dataclass(frozen=True)
class CostingMark:
    """Một dòng dấu bẩn như job đã đọc — `marked_at` là phiên bản.

    Một lượt ghi sổ chen vào giữa lúc job đang tính làm mới `marked_at`
    (`movements.mark_recalc`), và phép so-bằng khi xóa khiến dấu ấy sống sót
    để lượt sau tính lại — không bị coi là sạch oan.
    """

    branch_id: int
    warehouse_id: int
    item_id: int
    lot_key: int
    from_date: date
    marked_at: datetime


def pending_marks(session: Session, *, branch_id: int) -> tuple[CostingMark, ...]:
    rows = session.execute(
        select(
            InventoryRecalcMark.branch_id,
            InventoryRecalcMark.warehouse_id,
            InventoryRecalcMark.item_id,
            InventoryRecalcMark.lot_key,
            InventoryRecalcMark.from_date,
            InventoryRecalcMark.marked_at,
        )
        .where(InventoryRecalcMark.branch_id == branch_id)
        .order_by(
            InventoryRecalcMark.from_date,
            InventoryRecalcMark.warehouse_id,
            InventoryRecalcMark.item_id,
            InventoryRecalcMark.lot_key,
        )
    ).all()
    return tuple(
        CostingMark(
            branch_id=row.branch_id,
            warehouse_id=row.warehouse_id,
            item_id=row.item_id,
            lot_key=row.lot_key,
            from_date=row.from_date,
            marked_at=row.marked_at,
        )
        for row in rows
    )


def clear_marks(session: Session, marks: tuple[CostingMark, ...]) -> int:
    """Xóa đúng những phiên bản dấu đã đọc; dấu được làm mới thì để lại."""
    cleared = 0
    for mark in marks:
        result = cast(
            "CursorResult[Any]",
            session.execute(
                delete(InventoryRecalcMark).where(
                    InventoryRecalcMark.branch_id == mark.branch_id,
                    InventoryRecalcMark.warehouse_id == mark.warehouse_id,
                    InventoryRecalcMark.item_id == mark.item_id,
                    InventoryRecalcMark.lot_key == mark.lot_key,
                    InventoryRecalcMark.marked_at == mark.marked_at,
                )
            ),
        )
        cleared += result.rowcount
    return cleared
