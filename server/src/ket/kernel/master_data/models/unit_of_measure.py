"""Đơn vị tính (`docs/srs/01` §7, FR-SYS-041).

Danh mục dùng chung toàn hệ thống. Bảng **quy đổi** giữa các đơn vị (thùng → 24
chiếc) cố ý không nằm ở đây mà treo trên từng vật tư ở lát 3B-3: cùng một chữ
"thùng" là 24 chiếc với mặt hàng này và 12 với mặt hàng kia, nên một bảng quy đổi
toàn cục sẽ hoặc sai, hoặc phải nhân bản đơn vị theo từng mặt hàng — mà nhân bản
thì chính là bỏ danh mục dùng chung.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

UNIT_OF_MEASURE_TABLE_NAME = "units_of_measure"


class UnitOfMeasure(MasterDataRow):
    """Một đơn vị tính: chiếc, kg, mét, thùng, giờ công."""

    __tablename__ = UNIT_OF_MEASURE_TABLE_NAME
    __table_args__ = master_data_table_args(UNIT_OF_MEASURE_TABLE_NAME)
