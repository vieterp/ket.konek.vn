"""Kho (`docs/srs/01` §7).

Địa điểm lưu trữ. Là một trong ba thành phần khóa của bảng tồn kho mà LD-09 bắt
dựng ngay từ ngày đầu — `(kho, vật tư, lô/serial)` — nên nó phải có mặt trước
phân hệ kho ở phase 8, không phải cùng lúc.

Cột "tài khoản kho ngầm định" mà SRS §7 nêu **chưa** khai ở đây: đích của nó là
`chart_of_accounts`, một bảng thuộc gói cấu hình phase 5. Khai một cột khóa
ngoại trỏ vào bảng chưa tồn tại thì hoặc phải bỏ ràng buộc, hoặc phải đảo thứ tự
migration — cả hai đắt hơn một `ALTER TABLE` ở phase 5.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

WAREHOUSE_TABLE_NAME = "warehouses"


class Warehouse(MasterDataRow):
    """Một kho vật lý hoặc kho logic (hàng gửi bán, hàng đang đi đường)."""

    __tablename__ = WAREHOUSE_TABLE_NAME
    __table_args__ = master_data_table_args(WAREHOUSE_TABLE_NAME)
