"""Ký hiệu chấm công (`docs/srs/01` §7).

Ký hiệu trên bảng chấm công: `X` đi làm, `P` nghỉ phép, `Ô` ốm. Đầu vào của
phân hệ tiền lương ở phase 9.

Cách quy đổi mỗi ký hiệu thành ngày công và thành tiền **không** khai ở đây —
nó phụ thuộc quy định lương của từng doanh nghiệp và từng thời kỳ, tức là dữ
liệu có hiệu lực theo ngày, không phải một cột trên danh mục. Phase 9 sở hữu
phần đó cùng với bảng quy định lương.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

TIMEKEEPING_SYMBOL_TABLE_NAME = "timekeeping_symbols"


class TimekeepingSymbol(MasterDataRow):
    """Một ký hiệu dùng trên bảng chấm công."""

    __tablename__ = TIMEKEEPING_SYMBOL_TABLE_NAME
    __table_args__ = master_data_table_args(TIMEKEEPING_SYMBOL_TABLE_NAME)
