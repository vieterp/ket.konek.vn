"""Số lượng "đã hứa giao" của phân hệ bán (`CommitmentProvider`, U7, lát 8D).

**Hệ thống không có chứng từ đơn đặt hàng** — `sales_invoice_lines.order_id` chỉ
là id chiều phân tích và chưa phân hệ nào làm chủ danh mục ấy (nợ ghi ở phase
07). Nguồn cam kết thật mà dữ liệu hiện có trả lời được là chính chứng từ bán
**chưa rời kho** (quyết định user 2026-09-23), gồm hai tập:

* hóa đơn bán **đã cất chưa ghi sổ** — kế toán đã hứa với khách, hàng còn trong
  kho;
* hóa đơn bán **đã ghi sổ mà chưa có phiếu xuất sinh ra** — đúng tập nhóm
  `chua-xuat-kho` của BFF việc còn thiếu (8A): ghi sổ xong mà cầu mua/bán chưa
  dựng được phiếu kho thì hàng vẫn chưa rời kho.

Hóa đơn đã có phiếu xuất thì **không** còn là cam kết: số lượng ấy đã trừ vào
`on_hand` rồi, đếm thêm lần nữa là trừ hai lần.

Số lượng quy về **đơn vị chính** như mọi con số tồn kho (FR-STK-006); dòng khai
đơn vị chưa có tỷ lệ quy đổi bị bỏ qua thay vì cộng sai đơn vị.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from ket.kernel.master_data.models.item import INVENTORY_NATURES, Item
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.modules.sales.models import SalesInvoice, SalesInvoiceKind, SalesInvoiceLine
from ket.posting.contracts import POSTING_DOCUMENT_REGISTRY, Voucher
from ket.posting.documents.models import VoucherStatus

_ZERO = Decimal(0)
_ONE = Decimal(1)

INVENTORY_PERMISSION_MODULE = "inventory"
"""Tên module trong registry chứng từ của `posting` — không phải import."""

COMMITTED_KINDS = frozenset({SalesInvoiceKind.GOODS, SalesInvoiceKind.AGENCY})
"""Chỉ hai loại hóa đơn kiêm xuất kho; giảm giá / điều chỉnh không hứa giao gì."""


class SalesCommitmentProvider:
    """Bản cài `CommitmentProvider` của module `sales` — một câu gộp cho cả lô
    mã hàng (lưới tồn kho hỏi hàng trăm mã một lượt)."""

    def committed_quantities(
        self,
        session: Session,
        *,
        item_ids: Sequence[int],
        branch_id: int,
        warehouse_id: int | None = None,
    ) -> Mapping[int, Decimal]:
        wanted = sorted(set(item_ids))
        if not wanted:
            return {}
        # Loại chứng từ của phân hệ kho hỏi registry chứ không import module
        # kho (luật phụ thuộc #1) — và hỏi mỗi lượt vì registry nạp theo tiến
        # trình, không theo thời điểm import tệp này.
        stock_types = POSTING_DOCUMENT_REGISTRY.codes_of_module(INVENTORY_PERMISSION_MODULE)
        generated_header = aliased(Voucher)
        has_stock_voucher = exists().where(
            generated_header.source_document_id == Voucher.id,
            generated_header.document_type.in_(stock_types),
        )
        rows = session.execute(
            select(
                SalesInvoiceLine.item_id,
                SalesInvoiceLine.unit_id,
                SalesInvoiceLine.quantity,
                Item.base_unit_id,
                ItemUnit.factor,
            )
            .join(SalesInvoice, SalesInvoice.id == SalesInvoiceLine.voucher_id)
            .join(Voucher, Voucher.id == SalesInvoice.id)
            .join(Item, Item.id == SalesInvoiceLine.item_id)
            .outerjoin(
                ItemUnit,
                (ItemUnit.item_id == SalesInvoiceLine.item_id)
                & (ItemUnit.unit_id == SalesInvoiceLine.unit_id),
            )
            .where(
                SalesInvoiceLine.item_id.in_(wanted),
                SalesInvoiceLine.quantity.is_not(None),
                SalesInvoice.kind.in_(sorted(COMMITTED_KINDS)),
                Voucher.branch_id == branch_id,
                Item.nature.in_(sorted(nature.value for nature in INVENTORY_NATURES)),
                Voucher.status.in_(
                    (int(VoucherStatus.DA_CAT), int(VoucherStatus.DA_GHI_SO)),
                ),
                # Đã ghi sổ thì chỉ tính khi CHƯA có phiếu kho sinh ra; đã cất
                # thì luôn tính (chưa ghi sổ thì chưa có phiếu nào).
                (Voucher.status == int(VoucherStatus.DA_CAT)) | ~has_stock_voucher,
                *(
                    (SalesInvoiceLine.warehouse_id == warehouse_id,)
                    if warehouse_id is not None
                    else ()
                ),
            )
        ).all()
        committed: dict[int, Decimal] = {}
        for row in rows:
            if row.unit_id is not None and row.unit_id != row.base_unit_id:
                factor = row.factor
                if factor is None:
                    continue
            else:
                factor = _ONE
            committed[row.item_id] = committed.get(row.item_id, _ZERO) + row.quantity * factor
        return committed
