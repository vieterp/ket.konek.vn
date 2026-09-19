"""Phân hệ Mua hàng (SRS 05 §3) — hóa đơn mua + chi phí mua hàng.

Import gói này (qua `ket.model_registry`) là đăng ký vào hai registry, cùng
khuôn `modules/cash_book`:

* **phân quyền** — `purchase.invoice.*` đủ bộ hành vi chứng từ. Năm loại hóa
  đơn (hàng hóa, dịch vụ, tài sản, hàng đi đường, trả lại hàng) dùng chung một
  mã quyền và một dãy số `PUR`: chúng là cùng một nghiệp vụ nhìn ở năm góc,
  không phải năm phân hệ.
* **loại chứng từ của posting** — `PUR` với `build_request` (posting_mapper)
  và ba hook vòng đời: ghi sổ xong ghi khoản phải trả vào sổ phụ công nợ (hoặc
  giảm nợ hóa đơn gốc nếu là trả lại hàng) **và sinh phiếu kho** cho dòng qua
  kho (8A), bỏ ghi sổ gỡ cả hai, xóa thì trả bộ đếm tham chiếu danh mục.
* **nguồn dòng phiếu kho** — `InventoryLineSource` (8A): cùng bộ dòng mà hook
  gửi cho `InventoryPosting`, để guard tồn kho kêu trên chính hóa đơn.

Module này KHÔNG mở endpoint hành động riêng: ghi sổ / bỏ ghi sổ / xóa đi qua
`/api/v1/vouchers/{id}/actions/*` dùng chung, chính là nơi ba hook trên chạy.
Guard ngưỡng nợ (FR-SYS-032) đăng ký ở `receivables` — chủ sổ phụ — chứ không
ở đây, để nó soi mọi chứng từ làm tăng nợ, kể cả của phân hệ bán sau này.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.config.printing.subjects import REGISTRY as PRINT_SUBJECT_REGISTRY
from ket.kernel.config.printing.subjects import PrintSubject
from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    VOUCHER_ACTIONS,
    Action,
    DocumentType,
    permission_code,
)
from ket.modules.purchase.models import PURCHASE_DOCUMENT_TYPE
from ket.posting.contracts import (
    POSTING_DOCUMENT_REGISTRY,
    PostingDocumentType,
    PostingRequest,
)

PURCHASE_PERMISSION_MODULE = "purchase"
INVOICE_PERMISSION_CODE = "invoice"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=PURCHASE_PERMISSION_MODULE, code=INVOICE_PERMISSION_CODE, actions=VOUCHER_ACTIONS
    )
)


PAYABLE_STATEMENT_PRINT_CODE = "BBDC-TRA"
"""Mã bản in biên bản đối chiếu & xác nhận công nợ PHẢI TRẢ (SRS 05 §5 #10) —
đi đường mẫu in, tính tại chỗ từ dataset công nợ (7G-5); quyền in là quyền xem
hóa đơn mua."""

PRINT_SUBJECT_REGISTRY.register(
    PrintSubject(
        code=PAYABLE_STATEMENT_PRINT_CODE,
        title="Biên bản đối chiếu và xác nhận công nợ phải trả",
        view_permission=permission_code(
            PURCHASE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.VIEW
        ),
    )
)


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper."""
    from ket.modules.purchase.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.purchase.service import PurchaseInvoiceService

    PurchaseInvoiceService(session).sync_after_post(voucher_id, user_id=user_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.purchase.service import PurchaseInvoiceService

    PurchaseInvoiceService(session).clear_after_unpost(voucher_id, user_id=user_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.purchase.service import PurchaseInvoiceService

    PurchaseInvoiceService(session).release_usage(voucher_id)


POSTING_DOCUMENT_REGISTRY.register(
    PostingDocumentType(
        code=PURCHASE_DOCUMENT_TYPE,
        permission_module=PURCHASE_PERMISSION_MODULE,
        permission_name=INVOICE_PERMISSION_CODE,
        title="Hóa đơn mua hàng",
        build_request=_build_posting_request,
        after_post=_after_post,
        after_unpost=_after_unpost,
        before_delete=_before_delete,
        # Bản in hóa đơn mua thuộc lát báo cáo/in (7G).
        print_details=None,
    )
)


def _register_inventory_line_source() -> None:
    """Nguồn dòng phiếu kho (lát 8A, ADR-024) — guard tồn kho của module kho hỏi
    "hóa đơn này sẽ nhập/xuất gì" trước khi ghi sổ; import cục bộ cùng lối
    `_build_posting_request`."""
    from ket.kernel.protocols import PROVIDERS
    from ket.modules.purchase.inventory_lines import PurchaseInventoryLineSource

    PROVIDERS.register_inventory_line_source(PurchaseInventoryLineSource())


_register_inventory_line_source()
