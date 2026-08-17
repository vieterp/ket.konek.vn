"""Hợp đồng (`docs/srs/16`, LD-08, plan.md §Phạm vi v1).

Chiều phân tích lõi thứ sáu. **Tính năng** hợp đồng & ngân sách (SRS 16) hoãn
sau v1 — không có màn hình theo dõi tiến độ, không có đối chiếu ngân sách. Bảng
vẫn dựng từ phase 3 vì cột `contract_id` nằm trên dòng phát sinh sổ cái (phase
4), và cột ấy cần một đích khóa ngoại ngay khi nó ra đời.

Đó chính là lý do plan ghi "chiều `contract_id` có sẵn trong schema từ phase 3
nên thêm sau không phải migrate bảng phát sinh": thêm một cột vào `gl_postings`
sau khi khách hàng đã ghi vài trăm nghìn dòng là việc phải làm trong cửa sổ bảo
trì, còn bật một màn hình lên thì không.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

CONTRACT_TABLE_NAME = "contracts"


class Contract(MasterDataRow):
    """Hợp đồng kinh tế dùng làm chiều phân tích trên dòng phát sinh."""

    __tablename__ = CONTRACT_TABLE_NAME
    __table_args__ = master_data_table_args(CONTRACT_TABLE_NAME)
