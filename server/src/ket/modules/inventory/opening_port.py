"""Cổng nhóm 5 của số dư ban đầu — lớp tồn đầu kỳ thành sổ kho (RT-24, lát 8C-1).

Cài `posting.contracts.OpeningDetailPort` cho `OpeningDetailKind.STOCK`, đăng
ký ở `inventory/__init__`. Mỗi lớp (`opening_balance_stock_layers`) thành **một
movement nhập đã có giá**:

* `posting_date` = ngày đầu năm − 1, `period_id` = kỳ đầu năm — engine 8B đọc
  toàn bộ lịch sử trước `start` để dựng trạng thái mở đầu, `rebuild_balances`
  gộp "trước ngày đầu kỳ" thành tồn đầu, guard tồn và lưới tồn cộng movement
  không hỏi ngày; khóa kỳ 1 (trigger kỳ khóa theo `period_id`) = khóa số dư
  ban đầu, đúng `guard_opening_writable`. Ngày ấy có thể rơi vào kỳ 12 của năm
  trước nếu năm trước có kỳ mà chưa có sổ kho — báo cáo theo ngày của năm trước
  sẽ thấy tồn đầu năm sau; bất biến bên dưới loại ca năm trước có sổ kho.
* `sequence_in_day` theo `(ngày nhập NULLS FIRST, số phiếu, dòng)` — thứ tự lớp
  FIFO là thứ tự nhập thật, không phải thứ tự gõ.
* `cost_state = COSTED`: giá là dữ liệu người dùng khai, engine không tính lại;
  XK đích danh trỏ được lớp này như mọi lần nhập.

**Bất biến hai chiều**: nhập tay nhóm 5 chỉ khi chi nhánh **chưa có movement nào
trước ngày đầu năm** — có thì tồn đầu năm này là kết quả của lịch sử (8B #7) và
nhập thêm là đếm hai lần (`OpeningStockHistoryExistsError`); chiều ngược,
`movements.record_movements` từ chối ghi sổ movement có ngày ≤ ngày lớp đầu kỳ
của chi nhánh (`InventoryMovementBeforeOpeningStockError`, review 8C-1 H-3). Năm sau, `carry_forward`
chỉ ghi dòng nhóm 5 (giá trị + số lượng qua `annotate_carried`), không sinh lớp
hay movement.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, delete, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from ket.kernel.errors import OpeningStockHistoryExistsError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.modules.inventory.guards import refuse_when_movements_referenced
from ket.modules.inventory.lots import lot_id_for
from ket.modules.inventory.models import (
    CostState,
    InventoryMovement,
    MovementDirection,
)
from ket.modules.inventory.movements import (
    StockKey,
    lock_stock_keys,
    lot_key_of,
    mark_recalc,
    refuse_while_costing,
)
from ket.posting.contracts import OpeningStockLayer
from ket.posting.opening_balances.models import OpeningBalance, OpeningDetailKind

_ONE_DAY: Final[timedelta] = timedelta(days=1)


class InventoryOpeningPort:
    """Bản cài `OpeningDetailPort` cho tồn kho."""

    @property
    def kind(self) -> int:
        return OpeningDetailKind.STOCK

    def lot_id_for(self, session: Session, *, item_id: int, lot_no: str) -> int:
        lot_id = lot_id_for(session, item_id, lot_no)
        if lot_id is None:  # pragma: no cover - service chỉ gọi khi có số lô
            raise ValueError("Số lô trống")
        return lot_id

    def clear(self, session: Session, *, fiscal_year: FiscalYear, branch_id: int) -> int:
        """Gỡ movement của mọi lớp đầu kỳ (năm, chi nhánh); khóa còn movement khác
        trong năm nhận dấu tính lại từ ngày đầu năm (tồn đầu đổi → giá xuất cả năm
        đổi). Trigger kỳ khóa là chốt cuối nếu `guard_opening_writable` bị bỏ qua."""
        refuse_while_costing(session, branch_id)
        opening_date = fiscal_year.start_date - _ONE_DAY
        keys = {
            (int(row.branch_id), int(row.warehouse_id), int(row.item_id), int(row.lot_key))
            for row in session.execute(
                select(
                    InventoryMovement.branch_id,
                    InventoryMovement.warehouse_id,
                    InventoryMovement.item_id,
                    InventoryMovement.lot_key,
                ).where(
                    InventoryMovement.branch_id == branch_id,
                    InventoryMovement.opening_layer_id.is_not(None),
                    InventoryMovement.posting_date == opening_date,
                )
            ).all()
        }
        if not keys:
            return 0
        lock_stock_keys(session, keys)
        # Lớp đã bị phiếu xuất đích danh chỉ tới thì không rút được (review 8C-1
        # H-2): FK RESTRICT là hàng rào cuối, guard nói được phải sửa phiếu nào.
        refuse_when_movements_referenced(
            session,
            select(InventoryMovement.id).where(
                InventoryMovement.branch_id == branch_id,
                InventoryMovement.opening_layer_id.is_not(None),
                InventoryMovement.posting_date == opening_date,
            ),
        )
        # `Session.execute` khai `Result`; DML luôn là `CursorResult`, nơi
        # `rowcount` có thật (cùng lý do `posting.balances.recalc.clear_marks`).
        deleted = cast(
            "CursorResult[Any]",
            session.execute(
                delete(InventoryMovement).where(
                    InventoryMovement.branch_id == branch_id,
                    InventoryMovement.opening_layer_id.is_not(None),
                    InventoryMovement.posting_date == opening_date,
                )
            ),
        ).rowcount
        _mark_keys_with_later_movements(
            session, keys, fiscal_year=fiscal_year, reason="opening stock cleared"
        )
        return int(deleted)

    def materialize(
        self,
        session: Session,
        *,
        fiscal_year: FiscalYear,
        first_period: AccountingPeriod,
        branch_id: int,
        layers: Sequence[OpeningStockLayer],
    ) -> int:
        if not layers:
            return 0
        # Cùng khóa với job tính giá: hai lượt nhập tồn đầu (hai năm) của một chi
        # nhánh cũng xếp hàng qua đây — thông điệp "đang tính giá" khi đụng lượt
        # nhập khác là chấp nhận được (hiếm, và cùng cách xử lý: thử lại).
        refuse_while_costing(session, branch_id)
        opening_date = fiscal_year.start_date - _ONE_DAY
        # Lớp cũ của CHÍNH năm này đã bị `clear` gỡ trước khi tới đây (thứ tự
        # của `write_staged`), nên mọi movement còn lại trước ngày đầu năm — kể
        # cả lớp đầu kỳ của một năm trước — đều là lịch sử thật.
        has_history = session.scalar(
            select(
                exists().where(
                    InventoryMovement.branch_id == branch_id,
                    InventoryMovement.posting_date < fiscal_year.start_date,
                )
            )
        )
        if has_history:
            raise OpeningStockHistoryExistsError(
                "Chi nhánh đã có sổ kho trước ngày đầu năm — tồn đầu năm này là số chuyển "
                "từ năm trước (chức năng chuyển số dư), không nhập tay",
                fiscal_year_id=fiscal_year.id,
                branch_id=branch_id,
            )
        ordered = sorted(
            layers,
            key=lambda layer: (
                layer.received_on is not None,
                layer.received_on or opening_date,
                layer.receipt_no or "",
                layer.sort_order,
            ),
        )
        keys: set[StockKey] = {
            (branch_id, layer.warehouse_id, layer.item_id, lot_key_of(layer.lot_id))
            for layer in ordered
        }
        lock_stock_keys(session, keys)
        next_sequence: dict[StockKey, int] = {}
        for layer in ordered:
            key: StockKey = (branch_id, layer.warehouse_id, layer.item_id, lot_key_of(layer.lot_id))
            sequence = next_sequence.get(key)
            if sequence is None:
                current = session.scalar(
                    select(func.coalesce(func.max(InventoryMovement.sequence_in_day), 0)).where(
                        InventoryMovement.branch_id == branch_id,
                        InventoryMovement.warehouse_id == layer.warehouse_id,
                        InventoryMovement.item_id == layer.item_id,
                        InventoryMovement.lot_key == lot_key_of(layer.lot_id),
                        InventoryMovement.posting_date == opening_date,
                    )
                )
                sequence = int(current or 0) + 1
            next_sequence[key] = sequence + 1
            session.add(
                InventoryMovement(
                    voucher_id=None,
                    line_id=None,
                    opening_layer_id=layer.id,
                    branch_id=branch_id,
                    warehouse_id=layer.warehouse_id,
                    item_id=layer.item_id,
                    lot_id=layer.lot_id,
                    lot_key=lot_key_of(layer.lot_id),
                    posting_date=opening_date,
                    period_id=first_period.id,
                    sequence_in_day=sequence,
                    direction=MovementDirection.IN,
                    quantity=layer.quantity,
                    unit_cost=layer.unit_cost,
                    amount=layer.amount,
                    cost_state=CostState.COSTED,
                )
            )
        session.flush()
        _mark_keys_with_later_movements(
            session, keys, fiscal_year=fiscal_year, reason="opening stock imported"
        )
        return len(ordered)

    def annotate_carried(
        self,
        session: Session,
        *,
        source_year: FiscalYear,
        target_year: FiscalYear,
        branch_id: int,
    ) -> int:
        """Số lượng tồn cuối năm nguồn theo `(kho, mã hàng)` → `quantity`/
        `unit_price` của dòng nhóm 5 năm nhận. Dòng năm nhận không mang lô
        (`gl_postings` không có cột lô — xem `carry_forward.sql`), nên gộp mọi lô.
        Hàng giữ hộ đứng ngoài (không phải giá trị tồn)."""
        totals = (
            select(
                InventoryMovement.warehouse_id,
                InventoryMovement.item_id,
                func.sum(InventoryMovement.direction * InventoryMovement.quantity).label("qty"),
            )
            .where(
                InventoryMovement.branch_id == branch_id,
                InventoryMovement.posting_date <= source_year.end_date,
                InventoryMovement.is_custodial.is_(False),
            )
            .group_by(InventoryMovement.warehouse_id, InventoryMovement.item_id)
            .subquery()
        )
        # Một (kho, mã hàng) có hai dòng sổ cái khác TK (1561/1562, hay 156 →
        # 152 giữa năm) thì số lượng không chia được cho từng TK — để NULL thay
        # vì gán trọn cho cả hai (review 8C-1 M-3). Cột 4 lẻ: làm tròn tường minh.
        sibling = aliased(OpeningBalance)
        single_row = ~exists().where(
            sibling.fiscal_year_id == OpeningBalance.fiscal_year_id,
            sibling.ledger == OpeningBalance.ledger,
            sibling.branch_id == OpeningBalance.branch_id,
            sibling.detail_kind == OpeningDetailKind.STOCK,
            sibling.warehouse_id == OpeningBalance.warehouse_id,
            sibling.item_id == OpeningBalance.item_id,
            sibling.id != OpeningBalance.id,
        )
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(OpeningBalance)
                .where(
                    OpeningBalance.fiscal_year_id == target_year.id,
                    OpeningBalance.branch_id == branch_id,
                    OpeningBalance.detail_kind == OpeningDetailKind.STOCK,
                    OpeningBalance.warehouse_id == totals.c.warehouse_id,
                    OpeningBalance.item_id == totals.c.item_id,
                    single_row,
                )
                .values(
                    quantity=func.round(totals.c.qty, 4),
                    unit_price=func.round(
                        (OpeningBalance.debit - OpeningBalance.credit)
                        / func.nullif(totals.c.qty, 0),
                        4,
                    ),
                )
            ),
        )
        return int(result.rowcount or 0)


def _mark_keys_with_later_movements(
    session: Session, keys: set[StockKey], *, fiscal_year: FiscalYear, reason: str
) -> None:
    """Khóa nào đã có movement trong năm (hoặc sau) thì tồn đầu vừa đổi làm giá
    xuất của chúng đổi → dấu bẩn từ ngày đầu năm (kẹp kỳ mở như mọi dấu). Một
    câu cho cả tập khóa (review 8C-1 L-4), dấu ghi theo thứ tự sắp xếp."""
    if not keys:
        return
    branch_id = next(iter(keys))[0]
    with_later = {
        (int(row.branch_id), int(row.warehouse_id), int(row.item_id), int(row.lot_key))
        for row in session.execute(
            select(
                InventoryMovement.branch_id,
                InventoryMovement.warehouse_id,
                InventoryMovement.item_id,
                InventoryMovement.lot_key,
            )
            .where(
                InventoryMovement.branch_id == branch_id,
                InventoryMovement.posting_date >= fiscal_year.start_date,
            )
            .distinct()
        ).all()
    }
    for branch_id, warehouse_id, item_id, lot_key in sorted(keys & with_later):
        mark_recalc(
            session,
            branch_id=branch_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            lot_id=lot_key or None,
            from_date=fiscal_year.start_date,
            reason=reason,
        )


INVENTORY_OPENING_PORT: Final[InventoryOpeningPort] = InventoryOpeningPort()
