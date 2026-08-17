"""Loại công trình (`docs/srs/01` §7).

Danh mục riêng chứ không phải một nút nhóm trong `projects`: loại công trình
("dân dụng", "hạ tầng", "sửa chữa") là thuộc tính **phân loại** dùng để lọc báo
cáo, còn nhánh cha trong cây `projects` là quan hệ **cấu thành** (hạng mục thuộc
công trình nào). Gộp hai thứ vào một cây buộc người dùng phải chọn: hoặc mất
đường cộng tổng theo hạng mục, hoặc mất đường lọc theo loại.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

PROJECT_TYPE_TABLE_NAME = "project_types"


class ProjectType(MasterDataRow):
    """Phân loại công trình để lọc và nhóm trên báo cáo."""

    __tablename__ = PROJECT_TYPE_TABLE_NAME
    __table_args__ = master_data_table_args(PROJECT_TYPE_TABLE_NAME)
