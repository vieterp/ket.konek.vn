"""Chứng từ bán → phiếu kho (FR-SAL-003 "kiêm phiếu xuất kho", lát 8A) — một
hàm cho cả hai chiều, cùng khuôn `purchase/inventory_lines.py`.

Chỉ chứng từ bật `is_stock_issue` sinh phiếu: hóa đơn bán thường → phiếu
**xuất** mang cặp TK giá vốn của từng dòng (`cogs_account_id` /
`inventory_account_id` — "dữ liệu phase 8 đọc", 7C-2) và **không giá** (engine
8B tính rồi repost, lật `cogs_posted`); hàng bán bị trả lại (kind 2) → phiếu
**nhập** không giá (FR-STK-004, 8C). Giảm giá / điều chỉnh không chạm kho.
Chiều báo ngược (8B): engine gọi `sync_cost_posted(posted=…)` sau mỗi lượt ghi
lại giá vốn → `cogs_posted` theo trạng thái; bỏ ghi sổ hóa đơn hạ cờ
(`service.clear_after_unpost`).

Dòng thiếu kho không sinh — nhóm "chưa xuất kho" của BFF việc còn thiếu.
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
from ket.modules.sales.models import SalesInvoice, SalesInvoiceKind, SalesInvoiceLine
from ket.posting.contracts import Voucher

ISSUE_OPERATION = "xuat-ban-hang"
RETURN_RECEIPT_OPERATION = "nhap-hang-ban-tra-lai"

_ISSUE_KINDS = frozenset({SalesInvoiceKind.GOODS, SalesInvoiceKind.AGENCY})


def planned_movement(session: Session, voucher_id: UUID) -> PlannedMovement | None:
    """`None` khi không phải chứng từ bán, không "kiêm phiếu kho", hoặc không có
    dòng nào qua kho."""
    body = session.get(SalesInvoice, voucher_id)
    if body is None or not body.is_stock_issue:
        return None
    if body.kind in _ISSUE_KINDS:
        kind = InventoryMovementKind.ISSUE
    elif body.kind == SalesInvoiceKind.RETURN:
        kind = InventoryMovementKind.RECEIPT
    else:
        return None
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - FK một-một
        return None
    lines = (
        session.execute(
            select(SalesInvoiceLine)
            .where(SalesInvoiceLine.voucher_id == voucher_id)
            .order_by(SalesInvoiceLine.line_no)
        )
        .scalars()
        .all()
    )
    candidates: list[tuple[SalesInvoiceLine, int, int, int, Decimal]] = [
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
    is_issue = kind == InventoryMovementKind.ISSUE
    planned_lines = tuple(
        InventoryMovementLine(
            item_id=item_id,
            item_variant_id=line.variant_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            unit_id=unit_id,
            debit_account_id=debit,
            credit_account_id=credit,
            source_line_id=line.id,
        )
        for line, item_id, warehouse_id, unit_id, quantity in candidates
        if item_id in stocked
        for debit, credit in (_cogs_pair(line, is_issue),)
    )
    if not planned_lines:
        return None
    return PlannedMovement(
        kind=kind,
        lines=planned_lines,
        operation_code=ISSUE_OPERATION if is_issue else RETURN_RECEIPT_OPERATION,
        partner_id=body.customer_id,
        partner_kind=PartnerKind.CUSTOMER,
        description=voucher.description,
    )


class SalesInventoryLineSource:
    """Cài `kernel.protocols.InventoryLineSource` — đăng ký ở `sales/__init__`."""

    def planned_movement(self, session: Session, voucher_id: UUID) -> PlannedMovement | None:
        return planned_movement(session, voucher_id)

    def sync_cost_posted(self, session: Session, voucher_id: UUID, *, posted: bool) -> None:
        """Engine 8B vừa ghi lại giá vốn của phiếu xuất sinh từ hóa đơn này →
        `cogs_posted` theo đúng trạng thái (BR-SAL-01); lật cả hai chiều vì gỡ
        lớp nhập duy nhất kéo dòng xuất về chờ giá. Không phải hóa đơn bán →
        không làm gì."""
        body = session.get(SalesInvoice, voucher_id)
        if body is not None and body.cogs_posted != posted:
            body.cogs_posted = posted
            session.flush()


def _cogs_pair(line: SalesInvoiceLine, is_issue: bool) -> tuple[int | None, int | None]:
    """Cặp giá vốn chỉ ở chiều xuất, và đi cả đôi hoặc không đi (ràng buộc
    `account_pair_complete` của dòng phiếu): dòng chỉ khai một bên thì phiếu
    để trống cả hai — engine 8B sẽ đòi khi tính giá. Nhập lại theo FR-STK-004."""
    if not is_issue or line.cogs_account_id is None or line.inventory_account_id is None:
        return None, None
    return line.cogs_account_id, line.inventory_account_id
