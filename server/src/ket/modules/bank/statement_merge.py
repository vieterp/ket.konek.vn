"""Hook gộp `company_bank_accounts` — phần thuộc sao kê (lát 6D, FR-SYS-016).

Tồn tại vì `bank_statement_lines` mang unique
`(matched_voucher_id, bank_account_id)`: gộp hai tài khoản mà cùng MỘT chứng
từ đã khớp dòng ở CẢ HAI (chỉ xảy ra với chuyển nội bộ giữa đúng hai tài
khoản đang gộp) thì câu `UPDATE` vô danh của `merge_service` đổ bằng một
`IntegrityError` thô.

**Từ chối chứ không tự gỡ khớp**: hai tài khoản có chuyển nội bộ cho nhau mà
đem gộp thì chính chứng từ chuyển cũng thành chuyển-vào-chính-mình (CHECK
`counter_account_differs` chặn ở bước chuyển khóa ngoại của voucher) — trạng
thái này người dùng phải tự xử (xóa/bỏ ghi sổ chứng từ chuyển) chứ hệ thống
không đoán hộ; và một lần gỡ khớp im lặng trong lúc gộp là mất vết đối chiếu.

Đăng ký qua `CatalogRegistry.extend_merge_hooks` lúc import module (xem
`__init__.py`): bảng con thuộc module bank nên hook viết ở đây, kernel không
import ngược (luật phụ thuộc #5).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ket.kernel.errors import BankStatementMatchStateError
from ket.modules.bank.models import BankStatementLine
from ket.modules.bank.statement_branch import sync_statement_branch


class CompanyBankAccountStatementMergeHook:
    """Xem docstring module."""

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        source_line = aliased(BankStatementLine)
        target_line = aliased(BankStatementLine)
        conflict = session.execute(
            select(source_line.matched_voucher_id)
            .join(
                target_line,
                target_line.matched_voucher_id == source_line.matched_voucher_id,
            )
            .where(
                source_line.bank_account_id == source_id,
                target_line.bank_account_id == target_id,
                source_line.matched_voucher_id.is_not(None),
            )
            .limit(1)
        ).first()
        if conflict is not None:
            raise BankStatementMatchStateError(
                "Hai tài khoản có cùng chứng từ đã khớp sao kê ở cả hai bên (chuyển nội "
                "bộ giữa chúng) — gỡ khớp và xử lý chứng từ chuyển trước khi gộp",
                voucher_id=str(conflict[0]),
            )

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Chi nhánh của sao kê phải đi theo tài khoản ĐÍCH.

        Bộ gộp dùng chung vừa trỏ `bank_account_id` của sao kê + dòng sang
        `target_id`; nếu không đồng bộ tiếp thì chúng giữ chi nhánh của tài
        khoản vừa biến mất và RLS lọc theo một con số không còn nghĩa.
        """
        sync_statement_branch(session, bank_account_id=target_id)
