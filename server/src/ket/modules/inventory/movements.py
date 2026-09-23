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

**Dấu bẩn** (`inventory_recalc_queue`, SRS 19 §9 #6) ghi ở năm chỗ: ghi sổ một
phiếu mà đã có movement cùng khóa ở ngày **muộn hơn** (chèn lùi ngày — số thứ
tự mới luôn đứng cuối ngày nên cùng ngày không phải lùi; giá xuất FIFO/BQ tức
thời của các movement sau phải tính lại), ghi sổ hoặc gỡ một lần **nhập** khi
trước nó khóa đã xuất nhiều hơn nhập (lát 8B: các dòng xuất ấy "ăn" lớp nhập
sau hoặc giá lớp cuối), nhập vào kỳ đã có xuất cùng kỳ (8B: bình quân cuối kỳ
đổi cho cả kỳ), bỏ ghi sổ (lớp nhập biến mất), và đổi thứ tự trong ngày
(BR-STK-04). Upsert giữ `min(from_date)`: một dấu muộn không được che dấu sớm,
và `from_date` **kẹp vào kỳ mở sớm nhất** — số đã khóa là số đã chốt (xem
`mark_recalc`). 8A chỉ ghi dấu; engine 8B đọc và xóa.

**Trong lúc job tính giá đang chạy** (advisory lock chi nhánh, xem
`costing_lock_tag`), gỡ hay sắp xếp lại movement bị từ chối với 409 thay vì
chờ nhau chéo tới `DeadlockDetected` (job giữ row lock movement rồi xóa
`gl_postings`; bỏ ghi sổ xóa `gl_postings` rồi mới gỡ movement).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    InventoryCostingInProgressError,
    InventoryMovementBeforeOpeningStockError,
    PostingValidationError,
    PostingViolation,
)
from ket.kernel.money import convert_currency, round_money
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.periods.service import PeriodService, fiscal_year_covering
from ket.modules.inventory.models import (
    UNIT_COST_SCALE,
    CostState,
    InventoryMovement,
    InventoryRecalcMark,
    InventoryVoucher,
    InventoryVoucherKind,
    InventoryVoucherLine,
    MovementDirection,
    line_issues_stock,
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


def costing_lock_tag(schema: str, branch_id: int) -> str:
    """Khóa advisory của job tính giá một chi nhánh — job cầm cả lượt (blocking),
    đường ghi/gỡ movement thử cầm (`pg_try_advisory_xact_lock`) để biết job có
    đang chạy không."""
    return f"inventory.costing:{schema}:{branch_id}"


def refuse_while_costing(session: Session, branch_id: int) -> None:
    """Từ chối gỡ / sắp xếp lại movement khi job tính giá của chi nhánh đang
    chạy. Thử cầm khóa: được thì giữ tới hết transaction (job xếp hàng sau),
    không được nghĩa là job đang giữ → 409 có thông điệp, thay vì chờ chéo."""
    schema = session.scalar(text("SELECT current_schema()"))
    acquired = session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:tag)::bigint)"),
        {"tag": costing_lock_tag(str(schema), branch_id)},
    )
    if not acquired:
        raise InventoryCostingInProgressError(
            "Đang tính giá xuất kho cho chi nhánh này — chờ tác vụ xong rồi thử lại",
            branch_id=branch_id,
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
    _refuse_before_opening_stock(session, voucher)
    # Mọi khóa lấy TRƯỚC, theo thứ tự sắp xếp; số thứ tự cấp SAU, theo thứ tự dòng.
    lock_stock_keys(session, {stock_key_of(movement) for movement in created})
    by_warehouse = _costs_by_warehouse(session, voucher.posting_date)
    for movement in created:
        movement.sequence_in_day = _next_sequence(session, movement)
        if movement.is_custodial:
            # Không giá thì không có gì để tính lại: dấu bẩn cho khóa giữ hộ chỉ
            # làm hàng đợi không bao giờ cạn và chặn khóa sổ. Số thứ tự trong
            # ngày vẫn cấp — sổ kho số lượng cần thứ tự để in thẻ kho.
            session.add(movement)
            session.flush()
            continue
        if movement.direction == MovementDirection.IN and movement.source_movement_id is not None:
            _copy_cost_from_issue(session, movement, money_scale)
        if _has_later_movement(session, movement, by_warehouse=by_warehouse):
            mark_recalc(
                session,
                branch_id=movement.branch_id,
                warehouse_id=movement.warehouse_id,
                item_id=movement.item_id,
                lot_id=movement.lot_id,
                from_date=movement.posting_date,
                reason=f"backdated post {voucher.voucher_no}",
            )
        if movement.direction == MovementDirection.IN:
            # Nhập bù (8B): trước ngày này khóa đã xuất nhiều hơn nhập — những
            # dòng xuất ấy đang chờ giá hoặc đã "ăn" lớp nhập sau / giá lớp cuối
            # (luật FIFO vượt tồn), và lần nhập này đổi giá của chúng. Tính lại
            # từ dòng xuất sớm nhất của khóa.
            # Bình quân cuối kỳ: một lần nhập đổi giá của MỌI dòng xuất cùng kỳ,
            # kể cả dòng đã ghi sổ trước nó. Đánh dấu không phân biệt phương
            # pháp — module sổ kho không biết năm tính giá kiểu gì, và một lượt
            # tính lại thừa trên một khóa rẻ hơn một giá kỳ sai.
            earliest = min(
                (
                    d
                    for d in (
                        _shortage_before(session, movement, by_warehouse=by_warehouse),
                        _issue_in_same_period_before(session, movement, by_warehouse=by_warehouse),
                    )
                    if d is not None
                ),
                default=None,
            )
            if earliest is not None:
                mark_recalc(
                    session,
                    branch_id=movement.branch_id,
                    warehouse_id=movement.warehouse_id,
                    item_id=movement.item_id,
                    lot_id=movement.lot_id,
                    from_date=earliest,
                    reason=f"receipt after issues {voucher.voucher_no}",
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
    if rows:
        refuse_while_costing(session, voucher.branch_id)
    # Khóa giữ hộ không sinh dấu bẩn lúc ghi sổ, nên cũng không sinh lúc gỡ —
    # ngược lại thì một phiếu giữ hộ bỏ ghi sổ để lại dấu không bao giờ cạn.
    costed = [row for row in rows if not row.is_custodial]
    keys = {(row.branch_id, row.warehouse_id, row.item_id, row.lot_id) for row in costed}
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
    # Gỡ một lớp nhập mà trước đó khóa đã xuất nhiều hơn nhập: các dòng xuất
    # sớm hơn đã "ăn" lớp này (FIFO) — cùng luật nhập bù, chiều ngược.
    by_warehouse = _costs_by_warehouse(session, voucher.posting_date)
    for row in costed:
        if row.direction != MovementDirection.IN:
            continue
        shortage_from = _shortage_before(session, row, by_warehouse=by_warehouse)
        if shortage_from is not None:
            mark_recalc(
                session,
                branch_id=row.branch_id,
                warehouse_id=row.warehouse_id,
                item_id=row.item_id,
                lot_id=row.lot_id,
                from_date=shortage_from,
                reason=f"unpost layer after shortage {voucher.voucher_no}",
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
    đúng phiên bản dấu nó đã đọc).

    `from_date` kẹp lên **ngày đầu kỳ mở sớm nhất** từ ngày ấy trở đi (review 8B
    C-1): luật nhập bù / cùng kỳ có thể trỏ về một dòng xuất nằm trong kỳ đã
    khóa (tồn âm là chuyện thường khi guard ở mức `none`), và một dấu như thế
    làm engine từ chối cả chi nhánh vĩnh viễn (RT-11 phương án A). Số đã khóa
    là số đã chốt — phần chênh chảy vào dòng xuất kế tiếp còn mở, cùng chính
    sách với ranh giới năm.
    """
    from_date = _earliest_open_date(session, from_date)
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
    refuse_while_costing(session, payload.branch_id)
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
    """`(chiều, kho)` cho từng movement mà một dòng sinh ra.

    Chuyển kho: hai vế cùng dòng. Lắp ráp / tháo dỡ (8C-2): một vế theo vai dòng
    (`line_issues_stock`), tại kho của dòng — linh kiện và thành phẩm có thể ở
    kho khác nhau.
    """
    if body.kind == InventoryVoucherKind.TRANSFER:
        if body.to_warehouse_id is None:  # pragma: no cover - CHECK transfer_has_destination
            raise RuntimeError("Phiếu chuyển kho thiếu kho đến")
        return (
            (MovementDirection.OUT, body.warehouse_id),
            (MovementDirection.IN, body.to_warehouse_id),
        )
    if line_issues_stock(body.kind, is_product=line.is_product):
        return ((MovementDirection.OUT, line.warehouse_id),)
    return ((MovementDirection.IN, line.warehouse_id),)


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
    # giá gõ trên phiếu chuyển không có nghĩa nghiệp vụ. Vế nhập của lắp ráp /
    # tháo dỡ cũng vậy (service đã từ chối giá ở mọi kind ≠ NK; ở đây canh lần
    # hai vì movement là bảng sự thật).
    if line.is_custodial:
        # BR-STK-07: hàng nhận giữ hộ không có giá vốn, và không bao giờ có.
        # `NOT_APPLICABLE` là trạng thái CUỐI, không phải "chờ": engine đã lọc
        # `is_custodial` ở mọi câu SQL, còn `lock_check` coi nó là đã chốt.
        cost_state = CostState.NOT_APPLICABLE
    elif line.unit_cost_fc is not None and body.kind == InventoryVoucherKind.RECEIPT:
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
        is_custodial=line.is_custodial,
        posting_date=voucher.posting_date,
        period_id=voucher.period_id,
        sequence_in_day=0,
        direction=direction,
        quantity=line.base_quantity,
        unit_cost=unit_cost,
        amount=amount,
        cost_state=cost_state,
        source_movement_id=line.source_movement_id,
        cost_object_id=line.cost_object_id,
        project_id=line.project_id,
        order_id=line.order_id,
        contract_id=line.contract_id,
    )


def _refuse_before_opening_stock(session: Session, voucher: Voucher) -> None:
    """Chiều ngược của bất biến tồn đầu kỳ nhập tay (`opening_port`, review 8C-1
    H-3): chi nhánh đã khai lớp đầu kỳ cho năm N (movement ngày N.start − 1)
    thì không ghi sổ movement nào có ngày ≤ ngày ấy — engine đọc cả lịch sử,
    số ấy sẽ cộng lên tồn đầu đã khai, im lặng."""
    latest_opening = session.scalar(
        select(func.max(InventoryMovement.posting_date)).where(
            InventoryMovement.branch_id == voucher.branch_id,
            InventoryMovement.opening_layer_id.is_not(None),
        )
    )
    if isinstance(latest_opening, date) and voucher.posting_date <= latest_opening:
        raise InventoryMovementBeforeOpeningStockError(
            "Chi nhánh đã nhập tay tồn đầu kỳ cho năm sau ngày này — xóa nhóm tồn kho "
            "của số dư ban đầu năm ấy rồi chuyển số dư từ năm trước, thay vì ghi sổ lùi",
            posting_date=voucher.posting_date.isoformat(),
            opening_date=latest_opening.isoformat(),
        )


def _copy_cost_from_issue(session: Session, movement: InventoryMovement, money_scale: int) -> None:
    """Lần nhập hàng bán trả lại lấy giá của lần xuất nó quay về (FR-STK-004,
    8C-1) ngay lúc ghi sổ nếu lần xuất đã có giá — bút toán Nợ 156 / Có 632 có
    từ lượt ghi sổ, không đợi job; engine (`return_in_legs.sql`) giữ hai bên
    khớp về sau. Lần xuất chưa có giá → nhập chờ giá như mọi lần nhập chưa giá."""
    source = session.get(InventoryMovement, movement.source_movement_id)
    if source is None or source.unit_cost is None:
        return
    movement.unit_cost = source.unit_cost
    movement.amount = round_money(movement.quantity * source.unit_cost, money_scale)
    movement.cost_state = CostState.COSTED


def _key_filter(movement: InventoryMovement) -> ColumnElement[bool]:
    return and_(
        InventoryMovement.branch_id == movement.branch_id,
        InventoryMovement.warehouse_id == movement.warehouse_id,
        InventoryMovement.item_id == movement.item_id,
        InventoryMovement.lot_key == movement.lot_key,
    )


def _cost_group_filter(movement: InventoryMovement, *, by_warehouse: bool) -> ColumnElement[bool]:
    """Khóa mà GIÁ của movement này phụ thuộc: theo kho như thường; năm bình
    quân "không theo kho" (FR-STK-007, 8C-1) thì mọi kho của (chi nhánh, mã
    hàng, lô) — một lần nhập ở kho A đổi giá xuất ở kho B, nên các phép dò
    "chèn lùi ngày / nhập bù / cùng kỳ" phải nhìn cả nhóm. Dấu bẩn vẫn ghi theo
    khóa kho của movement — câu SQL nhóm gom dấu của mọi kho."""
    if by_warehouse:
        return _key_filter(movement)
    return and_(
        InventoryMovement.branch_id == movement.branch_id,
        InventoryMovement.item_id == movement.item_id,
        InventoryMovement.lot_key == movement.lot_key,
    )


def _costs_by_warehouse(session: Session, posting_date: date) -> bool:
    """`False` chỉ khi năm chứa ngày này tính bình quân gộp mọi kho."""
    year = fiscal_year_covering(session, posting_date)
    return year is None or year.costing_by_warehouse


def _next_sequence(session: Session, movement: InventoryMovement) -> int:
    current = session.scalar(
        select(func.coalesce(func.max(InventoryMovement.sequence_in_day), 0)).where(
            _key_filter(movement), InventoryMovement.posting_date == movement.posting_date
        )
    )
    return int(current or 0) + 1


def _earlier_than(movement: InventoryMovement) -> ColumnElement[bool]:
    """Movement đứng TRƯỚC movement này trong thứ tự khóa `(ngày, thứ tự trong
    ngày)` — so bộ đôi chứ không so ngày (review 8B H-1): một lần nhập cùng
    ngày nhưng thứ tự sau vẫn đứng sau dòng xuất cùng ngày thứ tự trước."""
    return or_(
        InventoryMovement.posting_date < movement.posting_date,
        and_(
            InventoryMovement.posting_date == movement.posting_date,
            InventoryMovement.sequence_in_day < movement.sequence_in_day,
        ),
    )


def _issue_in_same_period_before(
    session: Session, movement: InventoryMovement, *, by_warehouse: bool
) -> date | None:
    """Ngày xuất sớm nhất của khóa trong CÙNG kỳ, đứng trước movement này."""
    value = session.scalar(
        select(func.min(InventoryMovement.posting_date)).where(
            _cost_group_filter(movement, by_warehouse=by_warehouse),
            InventoryMovement.period_id == movement.period_id,
            _earlier_than(movement),
            InventoryMovement.direction == MovementDirection.OUT,
        )
    )
    return value if isinstance(value, date) else None


def _shortage_before(
    session: Session, movement: InventoryMovement, *, by_warehouse: bool
) -> date | None:
    """Trước movement này, khóa đã xuất nhiều hơn nhập? → ngày xuất sớm nhất của
    khóa (điểm tính lại), không thì `None`."""
    group = _cost_group_filter(movement, by_warehouse=by_warehouse)
    signed = session.scalar(
        select(func.coalesce(func.sum(InventoryMovement.direction * InventoryMovement.quantity), 0))
        .where(group, _earlier_than(movement))
        .where(InventoryMovement.is_custodial.is_(False))
    )
    if signed is None or signed >= 0:
        return None
    value = session.scalar(
        select(func.min(InventoryMovement.posting_date)).where(
            group,
            _earlier_than(movement),
            InventoryMovement.direction == MovementDirection.OUT,
        )
    )
    return value if isinstance(value, date) else None


def _earliest_open_date(session: Session, from_date: date) -> date:
    """`from_date` hoặc ngày đầu của kỳ MỞ sớm nhất kết thúc từ `from_date` trở
    đi (kỳ chưa khóa, năm chưa quyết toán) — tùy cái nào muộn hơn."""
    open_from = session.scalar(
        select(func.min(AccountingPeriod.start_date))
        .join(FiscalYear, FiscalYear.id == AccountingPeriod.fiscal_year_id)
        .where(
            AccountingPeriod.locked_at.is_(None),
            FiscalYear.is_closed.is_(False),
            AccountingPeriod.end_date >= from_date,
        )
    )
    if not isinstance(open_from, date):
        return from_date
    return max(from_date, open_from)


def _has_later_movement(
    session: Session, movement: InventoryMovement, *, by_warehouse: bool
) -> bool:
    """Đã có movement cùng khóa giá ở ngày muộn hơn? — chèn lùi ngày (§9 #6)."""
    # Nhóm nhiều kho (không theo kho): số thứ tự trong ngày cấp theo kho nên
    # một lần nhập cùng ngày ở kho khác có thể đứng TRƯỚC dòng xuất đã tính
    # (thứ tự nhóm là `(ngày, thứ tự, kho, id)`) — coi cùng ngày là "lùi", dấu
    # thừa vô hại (review 8C-1 M-2).
    later = session.scalar(
        select(
            exists().where(
                _cost_group_filter(movement, by_warehouse=by_warehouse),
                InventoryMovement.posting_date > movement.posting_date
                if by_warehouse
                else InventoryMovement.posting_date >= movement.posting_date,
            )
        )
    )
    return bool(later)
