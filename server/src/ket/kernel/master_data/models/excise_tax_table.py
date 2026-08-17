"""Biểu thuế tiêu thụ đặc biệt (`docs/srs/01` §7).

Chỉ bảng đầu ở lát này (H50) — thuế suất theo mặt hàng chịu thuế TTĐB thuộc
phase 9, cùng lý do đã ghi ở `pit_table.py`.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

EXCISE_TAX_TABLE_TABLE_NAME = "excise_tax_tables"


class ExciseTaxTable(MasterDataRow):
    """Một biểu thuế TTĐB có hiệu lực trong một thời kỳ."""

    __tablename__ = EXCISE_TAX_TABLE_TABLE_NAME
    __table_args__ = master_data_table_args(EXCISE_TAX_TABLE_TABLE_NAME)
