"""Hook gộp danh mục cho sổ kho (FR-SYS-016, lát 8A) — gắn vào `items` và
`warehouses` từ module vì kernel không import ngược (cùng lối `bank`).

Bộ gộp dùng chung dời khóa ngoại bằng một `UPDATE` vô danh; với sổ kho thì
việc ấy **không có nghĩa nghiệp vụ**: hai chuỗi `sequence_in_day` của hai khóa
tồn kho đụng nhau (`uq_inventory_movements_day_sequence`), và giá xuất đã tính
theo lớp của khóa cũ không còn nói về thứ gì. Vì thế chính sách là **từ chối**
khi bản ghi nguồn còn dòng sổ kho — người dùng chuyển tồn sang kho/mã đích bằng
phiếu chuyển kho (hoặc xuất–nhập điều chỉnh) rồi mới gộp; sổ kho vì thế kể đúng
câu chuyện thay vì bị viết lại. Cùng doctrine với `invoice_form_service` (gộp ký
hiệu đã cấp số thì từ chối).

`lots` (unique theo `(item_id, lot_no)`) đi cùng: mã hàng nguồn còn lô là còn
lịch sử theo lô.
"""

from __future__ import annotations

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from ket.kernel.errors import MasterDataMergeRefusedError
from ket.modules.inventory.models import (
    InventoryMovement,
    InventoryRecalcMark,
    InventoryVoucher,
    Lot,
)


class ItemStockLedgerMergeHook:
    """`items`: từ chối khi mã hàng nguồn còn movement hoặc lô."""

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        if session.scalar(select(exists().where(InventoryMovement.item_id == source_id))):
            raise MasterDataMergeRefusedError(
                "Mã hàng nguồn còn dòng sổ kho — chuyển tồn sang mã đích bằng phiếu "
                "xuất/nhập điều chỉnh rồi mới gộp",
                reason="inventory_movements_present",
                entity_id=source_id,
            )
        if session.scalar(select(exists().where(Lot.item_id == source_id))):
            raise MasterDataMergeRefusedError(
                "Mã hàng nguồn còn số lô — lô là lịch sử theo mã hàng, không gộp được",
                reason="lots_present",
                entity_id=source_id,
            )
        # Khóa không còn movement thì không có gì để tính lại: dấu bẩn còn sót
        # (bỏ ghi sổ phiếu cuối cùng) mà để bộ gộp dời sang mã đích sẽ đụng
        # `uq_inventory_recalc_queue_key` khi đích đã có dấu (review 8A M-4).
        session.execute(delete(InventoryRecalcMark).where(InventoryRecalcMark.item_id == source_id))

    def after_move(self, session: Session, *, target_id: int) -> None:
        return None


class WarehouseStockLedgerMergeHook:
    """`warehouses`: từ chối khi kho nguồn còn movement."""

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        if session.scalar(select(exists().where(InventoryMovement.warehouse_id == source_id))):
            raise MasterDataMergeRefusedError(
                "Kho nguồn còn dòng sổ kho — chuyển tồn sang kho đích bằng phiếu chuyển "
                "kho rồi mới gộp",
                reason="inventory_movements_present",
                entity_id=source_id,
            )
        # Phiếu chuyển nháp nguồn→đích (hoặc ngược) sau gộp thành "chuyển kho
        # sang chính nó" và nổ `transfer_changes_warehouse` giữa lượt gộp.
        spanning = exists().where(
            or_(
                (InventoryVoucher.warehouse_id == source_id)
                & (InventoryVoucher.to_warehouse_id == target_id),
                (InventoryVoucher.warehouse_id == target_id)
                & (InventoryVoucher.to_warehouse_id == source_id),
            )
        )
        if session.scalar(select(spanning)):
            raise MasterDataMergeRefusedError(
                "Còn phiếu chuyển kho giữa kho nguồn và kho đích — xóa hoặc sửa phiếu ấy "
                "trước khi gộp",
                reason="transfer_between_source_and_target",
                entity_id=source_id,
            )
        session.execute(
            delete(InventoryRecalcMark).where(InventoryRecalcMark.warehouse_id == source_id)
        )

    def after_move(self, session: Session, *, target_id: int) -> None:
        return None
