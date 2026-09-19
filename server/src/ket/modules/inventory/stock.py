"""Sổ kho — chiều ĐỌC số lượng: tồn tại một mốc và tồn **thấp nhất** từ một
ngày trở đi (cho guard FR-STK-040/041).

Đọc thẳng `inventory_movements` bằng `SUM(direction × quantity)`: 8A chưa có
snapshot `inventory_balances` (8B), và với guard thì đường tính-thẳng là đường
đúng dù có snapshot — nó phải thấy movement vừa ghi trong cùng transaction.

`stock_floor_from` là bản kho của `cash_balance_floor_from` (6B, M-2): một
phiếu xuất lùi ngày trừ vào tồn của MỌI ngày về sau, nên "tồn tại ngày ghi
sổ" không đủ — phải là tồn thấp nhất từ ngày đó tới hết dữ liệu. Chạy dưới
RLS của người ghi sổ: tồn kho là theo chi nhánh (`branch_id` nằm trong khóa),
nên phạm vi RLS và phạm vi nghiệp vụ trùng nhau — không cần hàm SECURITY
DEFINER như guard ngưỡng nợ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ket.modules.inventory.models import CostState, InventoryMovement
from ket.modules.inventory.schemas import StockRow

_ZERO = Decimal(0)


def stock_rows(
    session: Session,
    *,
    as_of: date,
    branch_id: int | None = None,
    warehouse_id: int | None = None,
    item_id: int | None = None,
) -> list[StockRow]:
    """Tồn theo `(chi nhánh, kho, vật tư, lô)` tại cuối ngày `as_of`.

    Giá trị chỉ trả khi **mọi** movement của khóa đã tính giá — một khóa có
    dòng chưa tính mà vẫn in giá trị là in một con số thiếu, và con số thiếu
    trông giống hệt con số đúng.
    """
    signed_qty = func.sum(InventoryMovement.direction * InventoryMovement.quantity)
    signed_amount = func.sum(InventoryMovement.direction * InventoryMovement.amount)
    all_costed = func.bool_and(InventoryMovement.cost_state == CostState.COSTED)
    query = (
        select(
            InventoryMovement.branch_id,
            InventoryMovement.warehouse_id,
            InventoryMovement.item_id,
            InventoryMovement.lot_id,
            signed_qty.label("on_hand"),
            signed_amount.label("value"),
            all_costed.label("all_costed"),
        )
        .where(InventoryMovement.posting_date <= as_of)
        .where(InventoryMovement.is_custodial.is_(False))
        .group_by(
            InventoryMovement.branch_id,
            InventoryMovement.warehouse_id,
            InventoryMovement.item_id,
            InventoryMovement.lot_id,
        )
        .order_by(
            InventoryMovement.branch_id,
            InventoryMovement.warehouse_id,
            InventoryMovement.item_id,
            InventoryMovement.lot_id,
        )
    )
    if branch_id is not None:
        query = query.where(InventoryMovement.branch_id == branch_id)
    if warehouse_id is not None:
        query = query.where(InventoryMovement.warehouse_id == warehouse_id)
    if item_id is not None:
        query = query.where(InventoryMovement.item_id == item_id)
    return [
        StockRow(
            branch_id=row.branch_id,
            warehouse_id=row.warehouse_id,
            item_id=row.item_id,
            lot_id=row.lot_id,
            on_hand=Decimal(row.on_hand or 0),
            value=Decimal(row.value or 0) if row.all_costed else None,
        )
        for row in session.execute(query)
    ]


def stock_floor_from(
    session: Session,
    *,
    branch_id: int,
    warehouse_id: int,
    item_id: int,
    lot_key: int | None,
    from_date: date,
) -> Decimal:
    """Tồn **thấp nhất** của một khóa từ điểm chèn của một phiếu ghi vào
    `from_date` tới hết dữ liệu.

    `lot_key=None` = gộp mọi lô của mã hàng ở kho (ngưỡng tồn tối thiểu
    FR-STK-041 khai theo mã hàng, không theo lô) — một câu cửa sổ duy nhất thay
    vì một lượt mỗi lô, và min của tổng chứ không tổng của các min (review 8A M-5).

    Điểm chèn là **cuối ngày** `from_date`: movement mới nhận `sequence_in_day`
    sau mọi movement đã có trong ngày, nên tồn nó nhìn thấy là tồn cuối ngày ấy
    — không phải tồn đầu ngày (một phiếu nhập cùng ngày đã có sẵn thì được
    tính). Từ đó đi tiếp: min của tồn cuối ngày và mọi số dư chạy sau từng
    movement của các ngày sau. Một phiếu xuất ghi vào `from_date` trừ vào mọi
    điểm ấy như nhau, nên guard chỉ việc trừ phần sắp ghi khỏi con số này.
    """
    conditions = [
        InventoryMovement.branch_id == branch_id,
        InventoryMovement.warehouse_id == warehouse_id,
        InventoryMovement.item_id == item_id,
        InventoryMovement.is_custodial.is_(False),
    ]
    if lot_key is not None:
        conditions.append(InventoryMovement.lot_key == lot_key)
    key = and_(*conditions)
    signed = InventoryMovement.direction * InventoryMovement.quantity
    end_of_day = session.scalar(
        select(func.coalesce(func.sum(signed), 0)).where(
            key, InventoryMovement.posting_date <= from_date
        )
    )
    opening = Decimal(end_of_day or 0)
    running = (
        select(
            func.sum(signed)
            .over(
                order_by=(
                    InventoryMovement.posting_date,
                    InventoryMovement.sequence_in_day,
                    InventoryMovement.lot_key,
                )
            )
            .label("running")
        )
        .where(key, InventoryMovement.posting_date > from_date)
        .subquery()
    )
    lowest_delta = session.scalar(select(func.min(running.c.running)))
    if lowest_delta is None:
        return opening
    return min(opening, opening + Decimal(lowest_delta))


def on_hand_at(
    session: Session,
    *,
    branch_id: int,
    warehouse_id: int,
    item_id: int,
    lot_key: int,
    as_of: date,
) -> Decimal:
    """Tồn của một khóa tại cuối ngày `as_of` — cho guard tồn tối thiểu."""
    total = session.scalar(
        select(
            func.coalesce(func.sum(InventoryMovement.direction * InventoryMovement.quantity), 0)
        ).where(
            InventoryMovement.branch_id == branch_id,
            InventoryMovement.warehouse_id == warehouse_id,
            InventoryMovement.item_id == item_id,
            InventoryMovement.lot_key == lot_key,
            InventoryMovement.is_custodial.is_(False),
            InventoryMovement.posting_date <= as_of,
        )
    )
    return Decimal(total or 0) if total is not None else _ZERO
