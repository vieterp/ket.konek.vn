"""Hóa đơn mua → phiếu kho (FR-STK-011, lát 8A) — một hàm cho cả hai chiều.

`planned_movement` là thứ `purchase` nói với module kho về một hóa đơn: dòng
nào qua kho, nhập kho nào, bao nhiêu, giá trị bao nhiêu. Cùng một hàm phục vụ:

* chiều **đọc** — bản cài `InventoryLineSource` (guard tồn kho hỏi trước khi
  ghi sổ, `InventoryAccountWithoutMovementGuard` hỏi "có sinh phiếu không");
* chiều **ghi** — hook `after_post` gọi `InventoryPosting.create_movement` với
  đúng những dòng ấy.

Hai chiều đọc cùng một hàm nên không có cách nào guard soi một bộ dòng còn
phiếu sinh ra bộ khác.

Luật chọn dòng: có `item_id` + `warehouse_id` + `quantity` + `unit_id`, và mã
hàng có `nature ∈ INVENTORY_NATURES`. Dòng thiếu kho **không** sinh — đó là
nhóm "chưa nhập kho" của BFF việc còn thiếu. Giá trị nhập = `amount_fc +
landed_cost_fc` (chi phí mua hàng đã phân bổ, 7B) — số chủ, để giá trị sổ kho
bằng giá trị Nợ 15x của hóa đơn tới từng đồng (BR-STK-03).

Hóa đơn trả lại hàng (kind 4) sinh phiếu **xuất** không giá — giá xuất là việc
của engine 8B. Hàng đi đường (kind 3) không sinh phiếu: hàng chưa về kho; phiếu
nhập lập khi hàng về (nghiệp vụ `nhap-hang-dang-di-duong`).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.master_data.models.item import INVENTORY_NATURES, Item
from ket.kernel.protocols import (
    InventoryMovementKind,
    InventoryMovementLine,
    PlannedMovement,
)
from ket.modules.purchase.models import (
    PurchaseInvoice,
    PurchaseInvoiceKind,
    PurchaseInvoiceLine,
)
from ket.posting.contracts import Voucher

RECEIPT_OPERATION = "nhap-mua-hang"
RETURN_ISSUE_OPERATION = "xuat-tra-lai-hang-mua"

_STOCK_KINDS = frozenset({PurchaseInvoiceKind.GOODS, PurchaseInvoiceKind.RETURN})


def planned_movement(session: Session, voucher_id: UUID) -> PlannedMovement | None:
    """`None` khi không phải hóa đơn mua hoặc không có dòng nào qua kho."""
    body = session.get(PurchaseInvoice, voucher_id)
    if body is None or body.kind not in _STOCK_KINDS:
        return None
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - FK một-một
        return None
    lines = (
        session.execute(
            select(PurchaseInvoiceLine)
            .where(PurchaseInvoiceLine.voucher_id == voucher_id)
            .order_by(PurchaseInvoiceLine.line_no)
        )
        .scalars()
        .all()
    )
    # Bộ tứ (mã hàng, kho, ĐVT, số lượng) đọc về một lần cho mypy hẹp kiểu.
    candidates: list[tuple[PurchaseInvoiceLine, int, int, int, Decimal]] = [
        (line, line.item_id, line.warehouse_id, line.unit_id, line.quantity)
        for line in lines
        if line.item_id is not None
        and line.warehouse_id is not None
        and line.unit_id is not None
        and line.quantity is not None
    ]
    if not candidates:
        return None
    stocked = {
        item.id
        for item in session.execute(
            select(Item).where(Item.id.in_(sorted({item_id for _, item_id, *_ in candidates})))
        ).scalars()
        if item.nature in INVENTORY_NATURES and not item.is_group
    }
    is_return = body.kind == PurchaseInvoiceKind.RETURN
    planned_lines = tuple(
        InventoryMovementLine(
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            unit_id=unit_id,
            # Trả lại hàng: giá xuất do engine; nhập: thành tiền là số chủ.
            unit_price_fc=None if is_return else line.unit_price_fc,
            amount_fc=None if is_return else line.amount_fc + line.landed_cost_fc,
            source_line_id=line.id,
        )
        for line, item_id, warehouse_id, unit_id, quantity in candidates
        if item_id in stocked
    )
    if not planned_lines:
        return None
    return PlannedMovement(
        kind=InventoryMovementKind.ISSUE if is_return else InventoryMovementKind.RECEIPT,
        lines=planned_lines,
        operation_code=RETURN_ISSUE_OPERATION if is_return else RECEIPT_OPERATION,
        partner_id=body.vendor_id,
        partner_kind=PartnerKind.VENDOR,
        description=voucher.description,
    )


class PurchaseInventoryLineSource:
    """Cài `kernel.protocols.InventoryLineSource` — đăng ký ở `purchase/__init__`."""

    def planned_movement(self, session: Session, voucher_id: UUID) -> PlannedMovement | None:
        return planned_movement(session, voucher_id)
