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

from ket.kernel.config.printing.context import DocumentPrintDetails
from ket.kernel.security.permissions import (
    CATALOG_ACTIONS,
    VOUCHER_ACTIONS,
    Action,
    DocumentType,
)
from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
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

STATEMENT_PERMISSION_CODE = "statement"
EBANKING_PERMISSION_CODE = "ebanking"

for _permission_code in (
    CREDIT_ADVICE_PERMISSION_CODE,
    PAYMENT_ORDER_PERMISSION_CODE,
    CHEQUE_PERMISSION_CODE,
    INTERNAL_TRANSFER_PERMISSION_CODE,
):
    PERMISSION_REGISTRY.register(
        DocumentType(module=BANK_PERMISSION_MODULE, code=_permission_code, actions=VOUCHER_ACTIONS)
    )

PERMISSION_REGISTRY.register(
    # Sao kê + đối chiếu (lát 6D): quyền riêng khỏi bốn loại chứng từ — người
    # đối chiếu không đương nhiên lập được ủy nhiệm chi. create = nhập sao kê,
    # edit = khớp/gỡ khớp, delete = xóa sao kê nhập nhầm.
    DocumentType(
        module=BANK_PERMISSION_MODULE, code=STATEMENT_PERMISSION_CODE, actions=CATALOG_ACTIONS
    )
)
PERMISSION_REGISTRY.register(
    # Ngân hàng điện tử (FR-BNK-020/021/022): v1 chưa có endpoint nào cầm mã
    # quyền này (kết nối/gửi lệnh ngoài v1 — phase file §Ngân hàng điện tử),
    # nhưng mã phải có từ bây giờ vì `requires_second_factor=True` là cách
    # FR-NFR-016 được thi hành: vai trò nào cấp quyền này thì người giữ bị bật
    # `totp_required` ngay lúc gán vai trò (`_role_requires_second_factor`) —
    # khai muộn cùng endpoint là mở đường cho một vai trò cấp trước khi luật 2FA
    # tồn tại. view = truy vấn số dư/sao kê trực tuyến, create = gửi lệnh chi.
    DocumentType(
        module=BANK_PERMISSION_MODULE,
        code=EBANKING_PERMISSION_CODE,
        actions=frozenset({Action.VIEW, Action.CREATE}),
        requires_second_factor=True,
    )
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
    from ket.modules.bank.reconciliation import ensure_not_matched_to_statement
    from ket.modules.bank.settlement_service import revert_settlements

    # H-3 review 6D: chứng từ đã khớp sao kê không bỏ ghi sổ được — hook chạy
    # cùng transaction với unpost nên ném ở đây là hủy cả lượt.
    ensure_not_matched_to_statement(session, voucher_id=voucher_id)
    revert_settlements(session, voucher_id=voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.bank.service import BankVoucherService

    BankVoucherService(session).release_usage(voucher_id)


def _print_details(session: Session, voucher_id: UUID, user_id: int) -> DocumentPrintDetails:
    """Trường riêng của chứng từ tiền gửi trên bản in (lát 6E-2) — import cục
    bộ cùng lối `_build_posting_request`."""
    from ket.modules.bank.print_details import build_print_details

    return build_print_details(session, voucher_id, user_id)


def _register_deposit_movement_source() -> None:
    """Bản cài `DepositMovementSource` (lát 6D) — carry-forward số dư đầu kỳ
    gọi qua kernel Protocol để giữ nhóm ngân hàng (kind 1) qua năm."""
    from ket.modules.bank.movement_source import register

    register()


_register_deposit_movement_source()


def _register_statement_merge_hook() -> None:
    """Hook gộp `company_bank_accounts` cho bảng con `bank_statement_lines`
    (unique theo tài khoản) — gắn từ module vì kernel không import ngược."""
    from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
    from ket.modules.bank.statement_merge import CompanyBankAccountStatementMergeHook

    CATALOG_REGISTRY.extend_merge_hooks(
        "company_bank_accounts", CompanyBankAccountStatementMergeHook()
    )


_register_statement_merge_hook()


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
            print_details=_print_details,
        )
    )
