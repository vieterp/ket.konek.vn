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
    REFERENCE_GUARDS,
    PostingDocumentType,
    PostingRequest,
)

BANK_PERMISSION_MODULE = "bank"
CREDIT_ADVICE_PERMISSION_CODE = "credit_advice"
PAYMENT_ORDER_PERMISSION_CODE = "payment_order"
CHEQUE_PERMISSION_CODE = "cheque"
INTERNAL_TRANSFER_PERMISSION_CODE = "internal_transfer"

STATEMENT_PERMISSION_CODE = "statement"
STATEMENT_PROFILE_PERMISSION_CODE = "statement_profile"
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
    # Hồ sơ định dạng sao kê (lát 6G-2, màn khai): quyền riêng khỏi `statement`
    # — nhập sao kê là việc hằng ngày, còn sửa cách đọc tệp đổi luật diễn giải
    # của MỌI lượt nhập sau đó, và sai ở đây (dấu thập phân, cột tiền) hỏng
    # **im lặng** ra một con số hợp lệ khác.
    DocumentType(
        module=BANK_PERMISSION_MODULE,
        code=STATEMENT_PROFILE_PERMISSION_CODE,
        # CHỈ `view` + `edit` — đúng số cửa có thật (review 6G-2 M-3). Ba cửa
        # ghi (khai/sửa/xóa) dùng CHUNG một mã `edit` vì cùng một rủi ro (đổi
        # luật đọc mọi tệp sao kê sau đó) và không có vai trò thực tế nào được
        # sửa mà không được khai. Đăng ký `create`/`delete` như bản đầu là để
        # ma trận phân quyền hứa hai mã không cửa nào đọc: cấp `create` xong
        # vẫn 403, còn `edit` thì mở cả tạo lẫn xóa.
        actions=frozenset({Action.VIEW, Action.EDIT}),
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
    from ket.modules.bank.settlement_service import revert_settlements

    revert_settlements(session, voucher_id=voucher_id)


def _before_delete(session: Session, voucher_id: UUID, user_id: int) -> None:
    from ket.modules.bank.service import BankVoucherService

    BankVoucherService(session).release_usage(voucher_id)


def _print_details(session: Session, voucher_id: UUID, user_id: int) -> DocumentPrintDetails:
    """Trường riêng của chứng từ tiền gửi trên bản in (lát 6E-2) — import cục
    bộ cùng lối `_build_posting_request`."""
    from ket.modules.bank.print_details import build_print_details

    return build_print_details(session, voucher_id, user_id)


def _register_statement_match_guard() -> None:
    """Luật "chứng từ đã khớp sao kê thì không bỏ ghi sổ / không xóa" (6D H-3)
    đăng ký ở bộ guard DÙNG CHUNG của posting, không ở hook riêng bốn loại
    chứng từ tiền gửi.

    Từ lát 6G-2 bàn khớp nhận cả phiếu quỹ nộp/rút tiền và bút toán GLE chạm
    112 (M-3), nên luật này không còn là việc riêng của phân hệ ngân hàng —
    nhưng bảng `bank_statement_lines` thì vẫn là của nó. Đăng ký từ phía chủ
    bảng giữ được cả hai: mọi loại chứng từ được canh, mà `cash_book` /
    `general_ledger` không phải biết tới phân hệ ngân hàng (luật C3).
    """
    from ket.modules.bank.reconciliation import ensure_not_matched_to_statement

    def _guard(session: Session, voucher_id: UUID) -> None:
        ensure_not_matched_to_statement(session, voucher_id=voucher_id)

    REFERENCE_GUARDS.register(_guard)


_register_statement_match_guard()


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
