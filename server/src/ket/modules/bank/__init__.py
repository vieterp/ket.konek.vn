"""Phân hệ Ngân hàng — tiền gửi (SRS 04), lát 6C.

Import gói này (qua `ket.model_registry`) là đăng ký vào ba registry, cùng
khuôn `modules/cash_book/__init__.py`:

* **phân quyền** — bốn mã loại chứng từ đủ bộ hành vi: `bank.credit_advice.*`
  (báo có), `bank.payment_order.*` (ủy nhiệm chi), `bank.cheque.*` (séc),
  `bank.internal_transfer.*` (chuyển tiền nội bộ);
* **loại chứng từ của posting** — BC/UNC/SEC/CTNB dùng chung một
  `build_request` (posting_mapper) và hai hook vòng đời đối trừ + hook trả bộ
  đếm tham chiếu (đếm cả `company_bank_accounts` — nợ 6A);
* guard cảnh báo "chi quá số dư TK ngân hàng" (FR-BNK-009) KHÔNG đăng ký ở
  đây: `CashBalanceGuard` (module quỹ) đã soi mọi chứng từ chạm TK 111x/112x
  theo tiền tố số hiệu — một guard thứ hai là hai cảnh báo cho một dòng.
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
from ket.posting.contracts import (
    POSTING_DOCUMENT_REGISTRY,
    PostingDocumentType,
    PostingRequest,
)

BANK_PERMISSION_MODULE = "bank"
CREDIT_ADVICE_PERMISSION_CODE = "credit_advice"
PAYMENT_ORDER_PERMISSION_CODE = "payment_order"
CHEQUE_PERMISSION_CODE = "cheque"
INTERNAL_TRANSFER_PERMISSION_CODE = "internal_transfer"

for _permission_code in (
    CREDIT_ADVICE_PERMISSION_CODE,
    PAYMENT_ORDER_PERMISSION_CODE,
    CHEQUE_PERMISSION_CODE,
    INTERNAL_TRANSFER_PERMISSION_CODE,
):
    PERMISSION_REGISTRY.register(
        DocumentType(module=BANK_PERMISSION_MODULE, code=_permission_code, actions=VOUCHER_ACTIONS)
    )


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Import cục bộ để lúc `model_registry` nạp gói không kéo theo mapper —
    cùng lối PT/PC: đăng ký chỉ cần tên callable."""
    from ket.modules.bank.posting_mapper import build_posting_request

    return build_posting_request(session, voucher_id)


def _after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.bank.settlement_service import apply_settlements

    apply_settlements(session, voucher_id=voucher_id)


def _after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.bank.settlement_service import revert_settlements

    revert_settlements(session, voucher_id=voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.bank.service import BankVoucherService

    BankVoucherService(session).release_usage(voucher_id)


for _code, _permission_name, _title in (
    ("BC", CREDIT_ADVICE_PERMISSION_CODE, "Thu tiền gửi (giấy báo có)"),
    ("UNC", PAYMENT_ORDER_PERMISSION_CODE, "Chi tiền gửi (ủy nhiệm chi)"),
    ("SEC", CHEQUE_PERMISSION_CODE, "Séc chuyển khoản / séc tiền mặt"),
    ("CTNB", INTERNAL_TRANSFER_PERMISSION_CODE, "Chuyển tiền nội bộ"),
):
    POSTING_DOCUMENT_REGISTRY.register(
        PostingDocumentType(
            code=_code,
            permission_module=BANK_PERMISSION_MODULE,
            permission_name=_permission_name,
            title=_title,
            build_request=_build_posting_request,
            after_post=_after_post,
            after_unpost=_after_unpost,
            before_delete=_before_delete,
        )
    )
