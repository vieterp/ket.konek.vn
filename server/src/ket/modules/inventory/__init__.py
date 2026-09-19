"""Phân hệ Kho (SRS 09) — phiếu nhập/xuất/chuyển kho + sổ kho (lát 8A).

Import gói này (qua `ket.model_registry`) là đăng ký vào NĂM registry, cùng
khuôn `modules/cash_book`:

* **phân quyền** — `inventory.receipt.*` (NK), `inventory.issue.*` (XK),
  `inventory.transfer.*` (CK) đủ bộ hành vi chứng từ;
* **loại chứng từ của posting** — ba mã dùng chung một `build_request`
  (posting_mapper) và ba hook: ghi sổ xong dựng `inventory_movements`, bỏ ghi
  sổ gỡ ra, xóa thì trả bộ đếm tham chiếu;
* **guard cảnh báo** — ba guard FR-STK-040/041/042 soi mọi chứng từ làm tồn
  giảm hoặc chạm TK kho, không riêng phiếu của module này;
* **guard bỏ ghi sổ + sửa/xóa** — phiếu sinh từ chứng từ nguồn đứng yên chừng
  nào nguồn còn ghi sổ (`EDIT_GUARDS` + `REFERENCE_GUARDS`);
* **mục kiểm khóa sổ** — kỳ không khóa được khi giá xuất kho chưa chốt
  (`LOCK_CHECKS`, BR-STK-05);
* **bản cài `InventoryPosting`** (kernel Protocol) — cửa cho mua/bán sinh và gỡ
  phiếu kho;
* **engine tính giá** (lát 8B) — quyền `inventory.costing.{view,create}` và job
  `inventory.costing.recalc` (`costing/job.py`);
* **cổng nhóm 5 của số dư ban đầu** (lát 8C-1) — `OPENING_DETAIL_PORTS`: lớp
  tồn đầu kỳ thành movement nhập đã có giá (`opening_port.py`).

Module KHÔNG mở endpoint hành động riêng: ghi sổ / bỏ ghi sổ / xóa đi qua
`/api/v1/vouchers/{id}/actions/*` dùng chung, nơi ba hook trên chạy.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    VOUCHER_ACTIONS,
    Action,
    DocumentType,
)
from ket.modules.inventory.costing import COSTING_PERMISSION_CODE, COSTING_PERMISSION_MODULE
from ket.modules.inventory.guards import (
    InventoryAccountWithoutMovementGuard,
    StockBelowMinGuard,
    StockNegativeGuard,
    refuse_when_named_as_specific_source,
    refuse_when_source_posted,
)
from ket.modules.inventory.lock_check import ensure_inventory_costed
from ket.modules.inventory.models import (
    ISSUE_DOCUMENT_TYPE,
    RECEIPT_DOCUMENT_TYPE,
    TRANSFER_DOCUMENT_TYPE,
)
from ket.posting.contracts import (
    EDIT_GUARDS,
    GUARD_REGISTRY,
    LOCK_CHECKS,
    POSTING_DOCUMENT_REGISTRY,
    REFERENCE_GUARDS,
    PostingDocumentType,
    PostingRequest,
)

INVENTORY_PERMISSION_MODULE = "inventory"
RECEIPT_PERMISSION_CODE = "receipt"
ISSUE_PERMISSION_CODE = "issue"
TRANSFER_PERMISSION_CODE = "transfer"

for _permission_code in (RECEIPT_PERMISSION_CODE, ISSUE_PERMISSION_CODE, TRANSFER_PERMISSION_CODE):
    PERMISSION_REGISTRY.register(
        DocumentType(
            module=INVENTORY_PERMISSION_MODULE, code=_permission_code, actions=VOUCHER_ACTIONS
        )
    )
# Tính giá xuất kho: xem trước (FR-STK-003) và chạy job — cùng khuôn
# `posting.balance` (view/create), không phải một chứng từ.
PERMISSION_REGISTRY.register(
    DocumentType(
        module=COSTING_PERMISSION_MODULE,
        code=COSTING_PERMISSION_CODE,
        actions=frozenset({Action.VIEW, Action.CREATE}),
    )
)


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper."""
    from ket.modules.inventory.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.inventory.service import InventoryVoucherService

    InventoryVoucherService(session).sync_after_post(voucher_id, user_id=user_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.inventory.service import InventoryVoucherService

    InventoryVoucherService(session).clear_after_unpost(voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.inventory.service import InventoryVoucherService

    InventoryVoucherService(session).release_usage(voucher_id)


for _code, _permission_name, _title in (
    (RECEIPT_DOCUMENT_TYPE, RECEIPT_PERMISSION_CODE, "Phiếu nhập kho"),
    (ISSUE_DOCUMENT_TYPE, ISSUE_PERMISSION_CODE, "Phiếu xuất kho"),
    (TRANSFER_DOCUMENT_TYPE, TRANSFER_PERMISSION_CODE, "Phiếu chuyển kho"),
):
    POSTING_DOCUMENT_REGISTRY.register(
        PostingDocumentType(
            code=_code,
            permission_module=INVENTORY_PERMISSION_MODULE,
            permission_name=_permission_name,
            title=_title,
            build_request=_build_posting_request,
            after_post=_after_post,
            after_unpost=_after_unpost,
            before_delete=_before_delete,
            # Mẫu in 01-VT/02-VT thuộc lát báo cáo/in của phase 8 (8F).
            print_details=None,
        )
    )

GUARD_REGISTRY.register(StockNegativeGuard())
GUARD_REGISTRY.register(StockBelowMinGuard())
GUARD_REGISTRY.register(InventoryAccountWithoutMovementGuard())
EDIT_GUARDS.register(refuse_when_source_posted)
REFERENCE_GUARDS.register(refuse_when_source_posted)
REFERENCE_GUARDS.register(refuse_when_named_as_specific_source)
LOCK_CHECKS.register(ensure_inventory_costed)


def _register_bridge() -> None:
    """Bản cài `InventoryPosting` — import cục bộ cùng lối `_build_posting_request`."""
    from ket.kernel.protocols import PROVIDERS
    from ket.modules.inventory.bridge import InventoryPostingBridge

    PROVIDERS.register_inventory_posting(InventoryPostingBridge())


_register_bridge()


def _register_merge_hooks() -> None:
    """Sổ kho là bảng con có ràng buộc duy nhất theo `items`/`warehouses` — danh
    mục phải khai hook gộp (cổng `test_master_data_merge.py`); gắn từ module
    cùng lối `bank`."""
    from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
    from ket.modules.inventory.merge_hooks import (
        ItemStockLedgerMergeHook,
        WarehouseStockLedgerMergeHook,
    )

    CATALOG_REGISTRY.extend_merge_hooks("items", ItemStockLedgerMergeHook())
    CATALOG_REGISTRY.extend_merge_hooks("warehouses", WarehouseStockLedgerMergeHook())


_register_merge_hooks()


def _register_opening_port() -> None:
    """Cổng nhóm 5 của số dư ban đầu (8C-1): lớp tồn đầu kỳ → movement — `posting`
    gọi qua registry vì C4 cấm nó import module này."""
    from ket.modules.inventory.opening_port import INVENTORY_OPENING_PORT
    from ket.posting.contracts import OPENING_DETAIL_PORTS

    OPENING_DETAIL_PORTS.register(INVENTORY_OPENING_PORT)


_register_opening_port()


def _register_costing_job() -> None:
    """Loại job `inventory.costing.recalc` vào `REGISTRY` lúc import gói — worker
    và `/jobs/types` thấy nó cùng cơ chế `posting.balances.recalc_job`."""
    import ket.modules.inventory.costing.job  # noqa: F401 — đăng ký khi import


_register_costing_job()
