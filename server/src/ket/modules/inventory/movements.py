"""Sổ kho — chiều GHI: dựng/gỡ `inventory_movements` từ phiếu lúc ghi sổ / bỏ
ghi sổ, cấp `sequence_in_day`, đánh dấu tính lại, sắp xếp lại thứ tự trong ngày.

Cùng vai với `posting/engine/service._insert_postings` cho sổ cái: dòng phiếu
là thứ người dùng gõ, movement là thứ đã ghi — chỉ hai hook vòng đời (ghi sổ,
bỏ ghi sổ) và lượt sắp xếp lại (FR-STK-017) được chạm bảng này.

**`sequence_in_day` dưới advisory lock theo khóa** (RT-09): hai phiếu cùng khóa
`(chi nhánh, kho, vật tư, lô)` cùng ngày ghi sổ song song không được nhận cùng
số thứ tự — `max + 1` đọc rồi ghi là một cuộc đua kinh điển, và `UNIQUE` ở DB
chỉ biến cuộc đua thành một `IntegrityError` vô cớ cho người thứ hai.
`pg_advisory_xact_lock(hashtext(khóa))` xếp hàng họ lại; khóa nhả cùng
transaction nên không có đường quên nhả.

**Dấu bẩn** (`inventory_recalc_queue`, SRS 19 §9 #6) ghi ở ba chỗ: ghi sổ một
phiếu mà đã có movement cùng khóa ở ngày **muộn hơn hoặc bằng** (chèn lùi
ngày — giá xuất FIFO/BQ tức thời của các movement sau phải tính lại), bỏ ghi
sổ (lớp nhập biến mất), và đổi thứ tự trong ngày (BR-STK-04). Upsert giữ `min
(from_date)`: một dấu muộn không được che dấu sớm. 8A chỉ ghi dấu; engine 8B
đọc và xóa.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.money import convert_currency
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.periods.service import PeriodService
from ket.modules.inventory.models import (
    UNIT_COST_SCALE,
    CostState,
    InventoryMovement,
    InventoryRecalcMark,
    InventoryVoucher,
    InventoryVoucherKind,
    InventoryVoucherLine,
    MovementDirection,
)
from ket.modules.inventory.schemas import ReorderDayIn
from ket.posting.contracts import Voucher

REORDER_SET_MISMATCH_CODE = "inventory.reorder_set_mismatch"

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")
_RENUMBER_OFFSET = 1_000_000
"""Bước đệm khi đánh lại số thứ tự: UNIQUE kiểm từng dòng UPDATE nên đổi 1→2
trong khi 2 còn đó là vi phạm; đẩy cả ngày lên vùng cao rồi hạ về là hai lượt
UPDATE không bao giờ đụng nhau."""


def lot_key_of(lot_id: int | None) -> int:
    return lot_id or 0


StockKey = tuple[int, int, int, int]
"""`(branch_id, warehouse_id, item_id, lot_key)` — khóa tồn kho."""


def lock_stock_keys(session: Session, keys: set[StockKey]) -> None:
    """Xếp hàng mọi transaction cùng chạm các khóa tồn kho (RT-09).

    Khóa theo **thứ tự sắp xếp** — hai phiếu chạm cùng hai khóa theo thứ tự
    ngược nhau (chuyển kho A→B và B→A, hay ghi sổ [X, Y] trong khi bỏ ghi sổ
    {X, Y}) mà khóa theo thứ tự dòng là deadlock kinh điển (review 8A H-1).
    Khóa mang `current_schema()` vì advisory lock là phạm vi cả database: thiếu
    schema thì hai dataset trùng id chặn nhau oan (cùng doctrine
    `balances/recalc_job`).
    """
    if not keys:
        return
    schema = session.scalar(text("SELECT current_schema()"))
    for branch_id, warehouse_id, item_id, lot_key in sorted(keys):
        session.execute(
            _ADVISORY_LOCK,
            {"key": f"inventory:{schema}:{branch_id}:{warehouse_id}:{item_id}:{lot_key}"},
        )


def stock_key_of(movement: InventoryMovement) -> StockKey:
    return (movement.branch_id, movement.warehouse_id, movement.item_id, movement.lot_key)


def record_movements(
    session: Session,
    *,
    voucher: Voucher,
    body: InventoryVoucher,
    lines: list[InventoryVoucherLine],
    money_scale: int,
) -> list[InventoryMovement]:
    """Dựng movement cho một phiếu vừa ghi sổ — hook `after_post`.

    Phiếu chuyển sinh **cặp** cho mỗi dòng (ra kho đi, vào kho đến), cùng
    `line_id`; hai movement ấy nhận cùng giá khi engine 8B tính — BR-STK-06 là
    hệ quả cấu trúc. Giá: dòng có `unit_cost_fc` (nhập gõ tay, nhập từ hóa đơn
    mua) → `COSTED` với giá VND theo tỷ giá phiếu; còn lại `PENDING`.
    """
    created: list[InventoryMovement] = [
        _movement_for(voucher, body, line, direction, warehouse_id, money_scale)
        for line in lines
        for direction, warehouse_id in _legs(body, line)
    ]
    # Mọi khóa lấy TRƯỚC, theo thứ tự sắp xếp; số thứ tự cấp SAU, theo thứ tự dòng.
    lock_stock_keys(session, {stock_key_of(movement) for movement in created})
    for movement in created:
        movement.sequence_in_day = _next_sequence(session, movement)
        if _has_later_movement(session, movement):
            mark_recalc(
                session,
                branch_id=movement.branch_id,
                warehouse_id=movement.warehouse_id,
                item_id=movement.item_id,
                lot_id=movement.lot_id,
                from_date=movement.posting_date,
                reason=f"backdated post {voucher.voucher_no}",
            )
        session.add(movement)
        # Flush từng movement: `_next_sequence` của dòng sau cùng khóa cùng
        # ngày phải thấy số vừa cấp (autoflush lo việc này, nhưng nói rõ).
        session.flush()
    return created


def remove_movements(session: Session, *, voucher: Voucher) -> int:
    """Gỡ movement của một phiếu vừa bỏ ghi sổ — hook `after_unpost`; đánh dấu
    tính lại từ ngày ghi sổ cho từng khóa bị chạm. Trả số dòng đã gỡ."""
    rows = (
        session.execute(select(InventoryMovement).where(InventoryMovement.voucher_id == voucher.id))
        .scalars()
        .all()
    )
    keys = {(row.branch_id, row.warehouse_id, row.item_id, row.lot_id) for row in rows}
    lock_stock_keys(session, {stock_key_of(row) for row in rows})
    for branch_id, warehouse_id, item_id, lot_id in sorted(
        keys, key=lambda key: (key[0], key[1], key[2], key[3] or 0)
    ):
        mark_recalc(
            session,
            branch_id=branch_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            lot_id=lot_id,
            from_date=voucher.posting_date,
            reason=f"unpost {voucher.voucher_no}",
        )
    session.execute(delete(InventoryMovement).where(InventoryMovement.voucher_id == voucher.id))
    session.flush()
    return len(rows)


def mark_recalc(
    session: Session,
    *,
    branch_id: int,
    warehouse_id: int,
    item_id: int,
    lot_id: int | None,
    from_date: date,
    reason: str,
) -> None:
    """Upsert dấu bẩn cho một khóa, giữ ngày **sớm nhất** và làm mới `marked_at`
    (cùng lý do với `posting.balances.recalc_queue.mark_dirty`: job chỉ được xóa
    đúng phiên bản dấu nó đã đọc)."""
    statement = (
        insert(InventoryRecalcMark)
        .values(
            branch_id=branch_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            lot_id=lot_id,
            lot_key=lot_key_of(lot_id),
            from_date=from_date,
            marked_at=text("clock_timestamp()"),
            reason=reason,
        )
        .on_conflict_do_update(
            constraint="uq_inventory_recalc_queue_key",
            set_={
                "from_date": func.least(InventoryRecalcMark.from_date, from_date),
                "marked_at": text("clock_timestamp()"),
                "reason": reason,
            },
        )
    )
    session.execute(statement)


def reorder_day(session: Session, payload: ReorderDayIn) -> tuple[int, date]:
    """FR-STK-017: gán lại `sequence_in_day` cho đúng tập movement của
    `(khóa, ngày)`, rồi đánh dấu cần tính lại từ ngày đó (BR-STK-04).

    Tập id phải **bằng đúng** tập hiện có: thiếu một movement là để nó giữ số
    cũ và đụng số mới của người khác; thừa một id là chạm movement ngoài khóa.
    Kỳ đã khóa → từ chối (BR-STK-05) — trigger DB cũng chặn, nhưng câu ở đây
    nói được lý do.
    """
    lot_key = lot_key_of(payload.lot_id)
    lock_stock_keys(session, {(payload.branch_id, payload.warehouse_id, payload.item_id, lot_key)})
    key_filter = and_(
        InventoryMovement.branch_id == payload.branch_id,
        InventoryMovement.warehouse_id == payload.warehouse_id,
        InventoryMovement.item_id == payload.item_id,
        InventoryMovement.lot_key == lot_key,
    )
    rows = (
        session.execute(
            select(InventoryMovement)
            .where(key_filter, InventoryMovement.posting_date == payload.posting_date)
            .order_by(InventoryMovement.sequence_in_day)
        )
        .scalars()
        .all()
    )
    current_ids = {row.id for row in rows}
    requested = set(payload.ordered_movement_ids)
    if current_ids != requested:
        raise PostingValidationError(
            "Danh sách thứ tự không khớp tập chứng từ của ngày",
            violations=[
                PostingViolation(
                    REORDER_SET_MISMATCH_CODE,
                    "Gửi đủ và đúng mọi dòng sổ kho của khóa tồn kho trong ngày",
                    missing=",".join(str(i) for i in sorted(current_ids - requested)),
                    unexpected=",".join(str(i) for i in sorted(requested - current_ids)),
                )
            ],
        )
    if rows:
        period = session.get(AccountingPeriod, rows[0].period_id)
        if period is not None:
            PeriodService(session).ensure_open(period)

    # Đếm TRƯỚC khi ghi: `update()` qua ORM đồng bộ luôn đối tượng trong phiên,
    # nên so sau lượt ghi là so số mới với chính nó.
    old_sequence = {row.id: row.sequence_in_day for row in rows}
    changed = sum(
        1
        for index, movement_id in enumerate(payload.ordered_movement_ids, start=1)
        if old_sequence[movement_id] != index
    )
    # Hai lượt UPDATE: đẩy lên vùng cao rồi hạ về đúng chỗ — xem `_RENUMBER_OFFSET`.
    for offset_pass in (True, False):
        for index, movement_id in enumerate(payload.ordered_movement_ids, start=1):
            new_value = index + _RENUMBER_OFFSET if offset_pass else index
            session.execute(
                update(InventoryMovement)
                .where(InventoryMovement.id == movement_id)
                .values(sequence_in_day=new_value)
            )
        session.flush()
    # Từ ngày này trở đi giá xuất không còn đáng tin (BR-STK-04) — kể cả khi
    # thứ tự không đổi thật: một lượt "sắp xếp lại" là một lời tuyên bố.
    session.execute(
        update(InventoryMovement)
        .where(key_filter, InventoryMovement.posting_date >= payload.posting_date)
        .where(InventoryMovement.cost_state == CostState.COSTED)
        .values(cost_state=CostState.STALE)
    )
    mark_recalc(
        session,
        branch_id=payload.branch_id,
        warehouse_id=payload.warehouse_id,
        item_id=payload.item_id,
        lot_id=payload.lot_id,
        from_date=payload.posting_date,
        reason="reorder day",
    )
    session.expire_all()
    return changed, payload.posting_date


# ------------------------------------------------------------------ nội bộ


def _legs(body: InventoryVoucher, line: InventoryVoucherLine) -> tuple[tuple[int, int], ...]:
    """`(chiều, kho)` cho từng movement mà một dòng sinh ra."""
    if body.kind == InventoryVoucherKind.RECEIPT:
        return ((MovementDirection.IN, line.warehouse_id),)
    if body.kind == InventoryVoucherKind.ISSUE:
        return ((MovementDirection.OUT, line.warehouse_id),)
    if body.to_warehouse_id is None:  # pragma: no cover - CHECK transfer_has_destination
        raise RuntimeError("Phiếu chuyển kho thiếu kho đến")
    return (
        (MovementDirection.OUT, body.warehouse_id),
        (MovementDirection.IN, body.to_warehouse_id),
    )


def _movement_for(
    voucher: Voucher,
    body: InventoryVoucher,
    line: InventoryVoucherLine,
    direction: int,
    warehouse_id: int,
    money_scale: int,
) -> InventoryMovement:
    unit_cost: Decimal | None = None
    amount: Decimal | None = None
    cost_state = CostState.PENDING
    # Chuyển kho lấy giá từ lớp xuất (engine 8B) kể cả khi dòng có gõ giá —
    # giá gõ trên phiếu chuyển không có nghĩa nghiệp vụ.
    if line.unit_cost_fc is not None and body.kind != InventoryVoucherKind.TRANSFER:
        unit_cost = convert_currency(line.unit_cost_fc, voucher.exchange_rate, UNIT_COST_SCALE)
        amount = convert_currency(
            line.amount_fc if line.amount_fc is not None else Decimal(0),
            voucher.exchange_rate,
            money_scale,
        )
        cost_state = CostState.COSTED
    return InventoryMovement(
        voucher_id=voucher.id,
        line_id=line.id,
        branch_id=voucher.branch_id,
        warehouse_id=warehouse_id,
        item_id=line.item_id,
        item_variant_id=line.item_variant_id,
        lot_id=line.lot_id,
        serial_id=line.serial_id,
        lot_key=lot_key_of(line.lot_id),
        posting_date=voucher.posting_date,
        period_id=voucher.period_id,
        sequence_in_day=0,
        direction=direction,
        quantity=line.base_quantity,
        unit_cost=unit_cost,
        amount=amount,
        cost_state=cost_state,
        cost_object_id=line.cost_object_id,
        project_id=line.project_id,
        order_id=line.order_id,
        contract_id=line.contract_id,
    )


def _key_filter(movement: InventoryMovement) -> ColumnElement[bool]:
    return and_(
        InventoryMovement.branch_id == movement.branch_id,
        InventoryMovement.warehouse_id == movement.warehouse_id,
        InventoryMovement.item_id == movement.item_id,
        InventoryMovement.lot_key == movement.lot_key,
    )


def _next_sequence(session: Session, movement: InventoryMovement) -> int:
    current = session.scalar(
        select(func.coalesce(func.max(InventoryMovement.sequence_in_day), 0)).where(
            _key_filter(movement), InventoryMovement.posting_date == movement.posting_date
        )
    )
    return int(current or 0) + 1


def _has_later_movement(session: Session, movement: InventoryMovement) -> bool:
    """Đã có movement cùng khóa ở ngày muộn hơn? — chèn lùi ngày (§9 #6)."""
    later = session.scalar(
        select(
            exists().where(
                _key_filter(movement), InventoryMovement.posting_date > movement.posting_date
            )
        )
    )
    return bool(later)
