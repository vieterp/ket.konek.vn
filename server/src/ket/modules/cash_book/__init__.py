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

from ket.kernel.config.printing.context import DocumentPrintDetails
from ket.kernel.config.printing.subjects import (
    REGISTRY as PRINT_SUBJECT_REGISTRY,
)
from ket.kernel.config.printing.subjects import (
    PrintSubject,
)
from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    VOUCHER_ACTIONS,
    Action,
    DocumentType,
    permission_code,
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
    from ket.modules.cash_book.treasurer_source import sync_after_post

    apply_settlements(session, voucher_id=voucher_id)
    # Thủ quỹ (lát 6C): xếp hàng đợi, hoặc phân hệ tắt thì vào thẳng sổ quỹ
    # (FR-WHK-021) — cùng transaction với ghi sổ kế toán.
    sync_after_post(session, voucher_id, user_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.cash_book.settlement_service import revert_settlements
    from ket.modules.cash_book.treasurer_source import clear_after_unpost

    revert_settlements(session, voucher_id=voucher_id)
    clear_after_unpost(session, voucher_id, user_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.cash_book.service import CashVoucherService

    CashVoucherService(session).release_usage(voucher_id)


def _print_details(session: Session, voucher_id: UUID, user_id: int) -> DocumentPrintDetails:
    """Trường riêng của 01-TT/02-TT trên bản in (lát 6E-2) — import cục bộ
    cùng lối `_build_posting_request`."""
    from ket.modules.cash_book.print_details import build_print_details

    return build_print_details(session, voucher_id, user_id)


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
            print_details=_print_details,
        )
    )

COUNT_SHEET_PRINT_CODE = "KKQ"
"""Mã bản in của biên bản kiểm kê quỹ trong `print_templates.document_type`."""

PRINT_SUBJECT_REGISTRY.register(
    PrintSubject(
        code=COUNT_SHEET_PRINT_CODE,
        title="Bảng kiểm kê quỹ",
        view_permission=permission_code(
            CASH_PERMISSION_MODULE, COUNT_SHEET_PERMISSION_CODE, Action.VIEW
        ),
    )
)

GUARD_REGISTRY.register(CashBalanceGuard())


def _register_treasurer_source() -> None:
    """Nguồn phiếu cho hàng đợi thủ quỹ (lát 6C) — import cục bộ cùng lối
    `_build_posting_request`: đăng ký lúc nạp gói, thân nạp khi cần."""
    from ket.kernel.protocols import PROVIDERS
    from ket.modules.cash_book.treasurer_source import CashTreasurerVoucherSource

    PROVIDERS.register_treasurer_voucher_source(CashTreasurerVoucherSource())


_register_treasurer_source()
