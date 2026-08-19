"""Bộ kiểm ghi sổ — chạy hết, trả **toàn bộ** vi phạm (phase-04 §Validator).

Không dừng ở lỗi đầu: người dùng sửa chứng từ một lượt thay vì gửi–sửa–gửi
từng vòng. Mỗi tệp trong gói là một nhóm kiểm trả `list[PostingViolation]`;
`run_validators` nối tất cả và **không** ném — ném hay không là quyết định của
`PostingService`, nơi còn giữ vi phạm "kỳ đã khóa" từ phép đọc `FOR SHARE`.

Hai phép kiểm của phase-04 **không** nằm ở đây, có chủ đích:

* *Kỳ đang mở* (#7) — sống trong `PostingService.post` vì nó phải đọc dòng kỳ
  bằng `FOR SHARE` (RT-09); một validator đọc thường sẽ đúng lúc test một
  luồng và sai lúc hai người bấm cùng giây.
* *Nợ–Có không cùng dương trên một dòng* (#8) — chặn từ biên `PostingLine`
  (Pydantic) và chốt bằng `CHECK` của bảng; tới được đây thì dữ liệu đã sạch.

Kiểm *số quy đổi client gửi khớp `round_money`* (#9) thuộc tầng API của module
(engine tự tính quy đổi, không nhận số ngoài — xem `requests.py`).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.errors import PostingViolation
from ket.posting.engine.prepared import PreparedLine
from ket.posting.engine.validators.account import check_accounts
from ket.posting.engine.validators.balanced import check_balanced
from ket.posting.engine.validators.dimension_required import check_required_dimensions

__all__ = ["PreparedLine", "run_validators"]


def run_validators(
    session: Session,
    *,
    lines: list[PreparedLine],
    accounts: dict[int, ChartOfAccount],
    active_package_id: int,
) -> list[PostingViolation]:
    """Chạy trọn bộ kiểm trên các dòng đã quy đổi của **cả hai sổ**."""
    violations: list[PostingViolation] = []
    violations.extend(check_balanced(lines))
    violations.extend(check_accounts(lines, accounts=accounts, active_package_id=active_package_id))
    violations.extend(check_required_dimensions(session, lines=lines, accounts=accounts))
    return violations
