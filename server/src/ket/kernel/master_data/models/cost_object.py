"""Đối tượng tập hợp chi phí (`docs/srs/01` §7, LD-08).

Là một trong sáu **chiều phân tích lõi** — sáu chiều có cột cố định trên
`gl_postings` chứ không nằm trong bảng chiều mở rộng (plan.md §Kiến trúc dữ
liệu). Lý do tách đôi như vậy: chiều lõi xuất hiện trên hầu hết báo cáo quản trị
và cần chỉ mục riêng, còn chiều đặc thù ngành thì hiếm và ít bản ghi.

Ở phase 9, giá thành giản đơn tập hợp chi phí **theo đối tượng này** rồi phân bổ
(LD-11), nên nó phải tồn tại trước posting engine chứ không đợi tới phân hệ giá
thành.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

COST_OBJECT_TABLE_NAME = "cost_objects"


class CostObject(MasterDataRow):
    """Nơi chi phí được gom về: một phân xưởng, một sản phẩm, một công đoạn."""

    __tablename__ = COST_OBJECT_TABLE_NAME
    __table_args__ = master_data_table_args(COST_OBJECT_TABLE_NAME)
