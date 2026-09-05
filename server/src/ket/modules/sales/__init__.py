"""Phân hệ Bán hàng (SRS 06 §3) — hóa đơn bán + công nợ phải thu.

Import gói này (qua `ket.model_registry`) là đăng ký vào hai registry, cùng
khuôn `modules/purchase`:

* **phân quyền** — `sales.invoice.*` đủ bộ hành vi chứng từ. Năm loại hóa đơn
  (hàng hóa, dịch vụ, trả lại hàng bán, giảm giá hàng bán, bán đại lý) dùng
  chung một mã quyền và một dãy số `SAL`: chúng là cùng một nghiệp vụ nhìn ở
  năm góc, không phải năm phân hệ.
* **loại chứng từ của posting** — `SAL` với `build_request` (posting_mapper) và
  ba hook vòng đời: ghi sổ xong ghi khoản phải thu vào sổ phụ công nợ (hoặc
  giảm nợ hóa đơn gốc nếu là trả lại / giảm giá), bỏ ghi sổ gỡ ra, xóa thì trả
  bộ đếm tham chiếu danh mục.

Module này KHÔNG mở endpoint hành động riêng: ghi sổ / bỏ ghi sổ / xóa đi qua
`/api/v1/vouchers/{id}/actions/*` dùng chung, chính là nơi ba hook trên chạy.
Guard ngưỡng nợ (FR-SYS-032, FR-SAL-034) đăng ký ở `receivables` — chủ sổ phụ
— và đã canh sẵn mọi chứng từ làm tăng nợ từ 7B, nên chứng từ bán được soi mà
lát này không thêm dòng nào.
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
from ket.modules.sales.models import SALES_DOCUMENT_TYPE
from ket.posting.contracts import (
    POSTING_DOCUMENT_REGISTRY,
    PostingDocumentType,
    PostingRequest,
)

SALES_PERMISSION_MODULE = "sales"
INVOICE_PERMISSION_CODE = "invoice"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=SALES_PERMISSION_MODULE, code=INVOICE_PERMISSION_CODE, actions=VOUCHER_ACTIONS
    )
)


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper."""
    from ket.modules.sales.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.sales.service import SalesInvoiceService

    SalesInvoiceService(session).sync_after_post(voucher_id, user_id=user_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.sales.service import SalesInvoiceService

    SalesInvoiceService(session).clear_after_unpost(voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.sales.service import SalesInvoiceService

    SalesInvoiceService(session).release_usage(voucher_id)


POSTING_DOCUMENT_REGISTRY.register(
    PostingDocumentType(
        code=SALES_DOCUMENT_TYPE,
        permission_module=SALES_PERMISSION_MODULE,
        permission_name=INVOICE_PERMISSION_CODE,
        title="Hóa đơn bán hàng",
        build_request=_build_posting_request,
        after_post=_after_post,
        after_unpost=_after_unpost,
        before_delete=_before_delete,
        # Bản in hóa đơn bán và phiếu giao hàng thuộc lát báo cáo/in (7G).
        print_details=None,
    )
)
