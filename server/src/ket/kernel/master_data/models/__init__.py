"""Bảng danh mục — một tệp cho mỗi danh mục.

Một tệp cho mỗi bảng chứ không một tệp `models.py` khổng lồ: tới cuối phase 3 ở
đây có hơn hai mươi danh mục, và những cái nặng nhất (vật tư hàng hóa với đơn vị
quy đổi, bảng giá nhiều mức, định mức nguyên vật liệu — FR-SYS-040..046) kéo theo
vài bảng con mỗi cái.

Lát 3A dựng khung (`base.py`, `tree_path.py`, `service.py`) cùng **hai** danh mục
thật để khung có người dùng thật thay vì một model chỉ-để-test: đối tượng tập hợp
chi phí và khoản mục chi phí. Cả hai là chiều phân tích lõi của LD-08, cả hai là
cây thuần không bảng con, và phase 4 cần chúng để gắn vào dòng phát sinh sổ cái.
Các danh mục còn lại thêm ở lát 3B.
"""

from __future__ import annotations

from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.models.expense_item import ExpenseItem

__all__ = ["CostObject", "ExpenseItem"]
