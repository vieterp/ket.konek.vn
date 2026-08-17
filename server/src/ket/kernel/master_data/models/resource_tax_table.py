"""Biểu thuế tài nguyên (`docs/srs/01` §7).

Chỉ bảng đầu ở lát này (H50) — thuế suất theo loại tài nguyên thuộc phase 9,
cùng lý do đã ghi ở `pit_table.py`.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

RESOURCE_TAX_TABLE_TABLE_NAME = "resource_tax_tables"


class ResourceTaxTable(MasterDataRow):
    """Một biểu thuế tài nguyên có hiệu lực trong một thời kỳ."""

    __tablename__ = RESOURCE_TAX_TABLE_TABLE_NAME
    __table_args__ = master_data_table_args(RESOURCE_TAX_TABLE_TABLE_NAME)
