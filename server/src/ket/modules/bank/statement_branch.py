"""Chiều chi nhánh của sao kê ngân hàng — một hàm, mọi cửa (lát 6G-1).

`bank_statements`/`bank_statement_lines` sinh ra ở 0016/0019 không có chiều chi
nhánh nào, nên `test_rls_policy_coverage` — vốn quét theo cột `branch_id` —
không nhìn thấy chúng và hai bảng đứng ngoài cô lập chi nhánh (review 6E-1
H-1). Migration 0022 thêm cột và bật policy; tệp này giữ cột ấy đúng.

**Nguồn sự thật là `company_bank_accounts.branch_id`**, không phải chi nhánh
của người bấm nút Nhập: sao kê là sổ CỦA tài khoản. Chi nhánh của người nhập là
một câu trả lời thứ hai cho cùng một câu hỏi, và hai câu trả lời sẽ lệch ngay
lần đầu một kế toán chi nhánh khác nhập hộ.

Hai cửa ghi cột này, cả hai gọi hàm dưới đây:

* nhập sao kê (`statement_import.import_statement`) — dòng vừa tạo;
* gộp danh mục TK ngân hàng (`statement_merge`) — sau khi bộ gộp dùng chung đã
  chuyển `bank_account_id` sang tài khoản đích, chi nhánh phải đi theo tài
  khoản MỚI, nếu không sao kê giữ chi nhánh của tài khoản đã biến mất.

Không có cửa thứ ba: `MasterDataService.update` không nhận `branch_id` (chi
nhánh của một dòng danh mục chỉ đặt lúc tạo), nên `company_bank_accounts` không
đổi chi nhánh tại chỗ được.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.modules.bank.models import BankStatement, BankStatementLine


def sync_statement_branch(session: Session, *, bank_account_id: int) -> None:
    """Đặt lại `branch_id` của mọi sao kê + dòng của một tài khoản ngân hàng.

    Set-based, không đọc-rồi-ghi từng dòng: một tài khoản có thể mang hàng chục
    sao kê × 500 dòng.
    """
    branch_id = session.execute(
        select(CompanyBankAccount.branch_id).where(CompanyBankAccount.id == bank_account_id)
    ).scalar_one()
    for model in (BankStatement, BankStatementLine):
        session.execute(
            update(model)
            .where(model.bank_account_id == bank_account_id)
            .values(branch_id=branch_id)
        )
