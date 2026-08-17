"""Khoản mục chi phí (`docs/srs/01` §7, LD-08).

Chiều phân tích lõi thứ hai được dựng ở lát 3A. Nó trả lời câu "tiền chi cho
**việc gì**" — điện nước, thuê mặt bằng, công tác phí — trong khi
`CostObject` trả lời "chi phí này thuộc **về đâu**". Hai câu hỏi độc lập nhau,
nên chúng là hai chiều chứ không phải hai cách gọi của một chiều: chi phí điện
(khoản mục) của phân xưởng 2 (đối tượng) là một ô trong bảng chéo mà báo cáo
quản trị nào cũng cần.

Cây khoản mục thường sâu (chi phí bán hàng → chi phí vận chuyển → cước nội
địa), nên `is_group` ở đây được dùng nhiều: chỉ nút lá mới nhận bút toán, còn
nút nhóm chỉ cộng tổng — xem `MasterDataGroupNotPostableError`.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

EXPENSE_ITEM_TABLE_NAME = "expense_items"


class ExpenseItem(MasterDataRow):
    """Một khoản mục chi phí trong cây khoản mục của doanh nghiệp."""

    __tablename__ = EXPENSE_ITEM_TABLE_NAME
    __table_args__ = master_data_table_args(EXPENSE_ITEM_TABLE_NAME)
