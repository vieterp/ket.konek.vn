"""Phân hệ Mua hàng (SRS 05 §3) — hóa đơn mua + chi phí mua hàng.

Import gói này (qua `ket.model_registry`) là đăng ký vào hai registry, cùng
khuôn `modules/cash_book`:

* **phân quyền** — `purchase.invoice.*` đủ bộ hành vi chứng từ. Năm loại hóa
  đơn (hàng hóa, dịch vụ, tài sản, hàng đi đường, trả lại hàng) dùng chung một
  mã quyền và một dãy số `PUR`: chúng là cùng một nghiệp vụ nhìn ở năm góc,
  không phải năm phân hệ.
* **loại chứng từ của posting** — `PUR` với `build_request` (posting_mapper)
  và ba hook vòng đời: ghi sổ xong ghi khoản phải trả vào sổ phụ công nợ (hoặc
  giảm nợ hóa đơn gốc nếu là trả lại hàng), bỏ ghi sổ gỡ ra, xóa thì trả bộ
  đếm tham chiếu danh mục.

Module này KHÔNG mở endpoint hành động riêng: ghi sổ / bỏ ghi sổ / xóa đi qua
`/api/v1/vouchers/{id}/actions/*` dùng chung, chính là nơi ba hook trên chạy.
Guard ngưỡng nợ (FR-SYS-032) đăng ký ở `receivables` — chủ sổ phụ — chứ không
ở đây, để nó soi mọi chứng từ làm tăng nợ, kể cả của phân hệ bán sau này.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    VOUCHER_ACTIONS,
    DocumentType,
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


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper."""
    from ket.modules.purchase.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.purchase.service import PurchaseInvoiceService

    PurchaseInvoiceService(session).sync_after_post(voucher_id, user_id=user_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.purchase.service import PurchaseInvoiceService

    PurchaseInvoiceService(session).clear_after_unpost(voucher_id)


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
