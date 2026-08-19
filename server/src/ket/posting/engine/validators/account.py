"""Kiểm tài khoản của từng dòng (BR-SYS-03, validator #3 và #4).

Bốn câu hỏi cho mỗi TK mà chứng từ chạm: có tồn tại không, có thuộc gói cấu
hình đang hiệu lực tại ngày hạch toán không, có đang hoạt động không, và có
phải TK tổng hợp không. Câu thứ hai là hệ quả của việc TK thuộc **gói** (LD-06):
một chứng từ năm 2026 trỏ vào TK của gói TT133 trong khi năm 2026 đang chạy
TT99 là hạch toán vào một hệ thống TK khác — cân đến đâu cũng vô nghĩa.
"""

from __future__ import annotations

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.errors import PostingViolation
from ket.posting.engine.prepared import PreparedLine


def check_accounts(
    lines: list[PreparedLine],
    *,
    accounts: dict[int, ChartOfAccount],
    active_package_id: int,
) -> list[PostingViolation]:
    violations: list[PostingViolation] = []
    for line in lines:
        account = accounts.get(line.source.account_id)
        if account is None:
            violations.append(
                PostingViolation(
                    "account.not_found",
                    "Tài khoản không tồn tại trong hệ thống tài khoản",
                    ledger=line.ledger,
                    line_no=line.line_no,
                    account_id=line.source.account_id,
                )
            )
            continue
        if account.package_id != active_package_id:
            violations.append(
                PostingViolation(
                    "account.wrong_package",
                    "Tài khoản không thuộc gói cấu hình hiệu lực tại ngày hạch toán",
                    ledger=line.ledger,
                    line_no=line.line_no,
                    account=account.code,
                )
            )
            continue
        if account.is_inactive:
            violations.append(
                PostingViolation(
                    "account.inactive",
                    "Tài khoản đã ngừng sử dụng",
                    ledger=line.ledger,
                    line_no=line.line_no,
                    account=account.code,
                )
            )
        if account.is_summary:
            violations.append(
                PostingViolation(
                    "account.summary",
                    "Tài khoản tổng hợp — hãy hạch toán vào tài khoản chi tiết của nó",
                    ledger=line.ledger,
                    line_no=line.line_no,
                    account=account.code,
                )
            )
    return violations
