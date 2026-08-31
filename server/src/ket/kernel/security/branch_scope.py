"""Phép kiểm "phạm vi request phủ MỌI chi nhánh chưa".

Một nghiệp vụ chỉ đúng khi nhìn thấy toàn bộ dữ liệu kế toán — khóa sổ, khớp
sao kê tự động, đối chiếu ngân hàng — không được chạy dưới một phạm vi RLS hẹp
hơn công ty. Chạy hẹp không sinh lỗi: nó sinh một con số **sai mà trông đúng**
(chênh lệch giả, ứng viên khớp duy nhất còn nhìn thấy), nên phép kiểm phải là
một cổng chặn chứ không một cảnh báo.

Tệp này giữ **một** bản của phần dễ sai: tập chi nhánh đem ra so là `branches`
đọc trực tiếp — bảng đó **không bật RLS** (`security/models.py`) nên câu đếm
thấy đủ, và nó gồm cả chi nhánh **ngừng hoạt động** (chi nhánh ngừng vẫn có
phát sinh lịch sử; loại nó ra là mở lại đúng lỗ hổng phép kiểm sinh ra để bịt).

Hàm trả về *phần còn thiếu* thay vì tự ném: mỗi nơi gọi có mã lỗi và câu chữ
riêng cho người dùng ("Khóa sổ cần…", "Khớp tự động cần…"), và gộp chúng làm
một thông điệp chung sẽ nói với kế toán một câu không dính gì tới việc họ đang
làm. Chỉ phần *luật* là dùng chung.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.persistence.unit_of_work import RequestScope
from ket.kernel.security.models import Branch

__all__ = ["missing_scope_branch_ids", "scope_covers_every_branch"]


def missing_scope_branch_ids(session: Session, scope: RequestScope) -> frozenset[int]:
    """Chi nhánh có trong dữ liệu nhưng KHÔNG có trong phạm vi request."""
    every_branch_id = set(session.execute(select(Branch.id)).scalars().all())
    return frozenset(every_branch_id - set(scope.branch_ids))


def scope_covers_every_branch(session: Session, scope: RequestScope) -> bool:
    """Dạng trả-lời của `missing_scope_branch_ids`, cho nơi cần *hỏi* thay vì
    *chặn* (ví dụ: danh mục báo cáo đánh dấu mục người dùng mở được)."""
    return not missing_scope_branch_ids(session, scope)
