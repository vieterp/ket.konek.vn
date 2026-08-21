"""Phân hệ Quỹ tiền mặt (SRS 03) — module nghiệp vụ đầu tiên trên posting engine.

Import gói này (qua `ket.model_registry`) là đăng ký vào BA registry, cùng
khuôn `modules/general_ledger/journal/__init__.py`:

* **phân quyền** — `cash_book.receipt.*` (PT), `cash_book.payment.*` (PC) đủ bộ
  hành vi chứng từ; `cash_book.count_sheet.*` (biên bản kiểm kê) chỉ
  view/create/delete — biên bản không sửa được (chứng cứ của một lần đếm) và
  không ghi sổ trực tiếp;
* **loại chứng từ của posting** — PT/PC dùng chung một `build_request`
  (posting_mapper) và ba hook vòng đời: ghi sổ xong cộng số đã trả vào đích
  đối trừ, bỏ ghi sổ gỡ ra, xóa thì trả bộ đếm tham chiếu danh mục;
* **guard cảnh báo** — `CashBalanceGuard` (FR-SYS-062 "chi quá tồn") soi mọi
  chứng từ chạm TK 111x/112x, không riêng phiếu của module này.
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
from ket.modules.cash_book.guards import CashBalanceGuard
from ket.posting.contracts import (
    GUARD_REGISTRY,
    POSTING_DOCUMENT_REGISTRY,
    PostingDocumentType,
    PostingRequest,
)

CASH_PERMISSION_MODULE = "cash_book"
RECEIPT_PERMISSION_CODE = "receipt"
PAYMENT_PERMISSION_CODE = "payment"
COUNT_SHEET_PERMISSION_CODE = "count_sheet"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=CASH_PERMISSION_MODULE, code=RECEIPT_PERMISSION_CODE, actions=VOUCHER_ACTIONS
    )
)
PERMISSION_REGISTRY.register(
    DocumentType(
        module=CASH_PERMISSION_MODULE, code=PAYMENT_PERMISSION_CODE, actions=VOUCHER_ACTIONS
    )
)
PERMISSION_REGISTRY.register(
    DocumentType(
        module=CASH_PERMISSION_MODULE,
        code=COUNT_SHEET_PERMISSION_CODE,
        actions=frozenset({Action.VIEW, Action.CREATE, Action.DELETE}),
    )
)


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper —
    cùng lối GLE: đăng ký chỉ cần tên callable."""
    from ket.modules.cash_book.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.cash_book.settlement_service import apply_settlements

    apply_settlements(session, voucher_id=voucher_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.cash_book.settlement_service import revert_settlements

    revert_settlements(session, voucher_id=voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.cash_book.service import CashVoucherService

    CashVoucherService(session).release_usage(voucher_id)


for _code, _permission_name, _title in (
    ("PT", RECEIPT_PERMISSION_CODE, "Phiếu thu tiền mặt"),
    ("PC", PAYMENT_PERMISSION_CODE, "Phiếu chi tiền mặt"),
):
    POSTING_DOCUMENT_REGISTRY.register(
        PostingDocumentType(
            code=_code,
            permission_module=CASH_PERMISSION_MODULE,
            permission_name=_permission_name,
            title=_title,
            build_request=_build_posting_request,
            after_post=_after_post,
            after_unpost=_after_unpost,
            before_delete=_before_delete,
        )
    )

GUARD_REGISTRY.register(CashBalanceGuard())
