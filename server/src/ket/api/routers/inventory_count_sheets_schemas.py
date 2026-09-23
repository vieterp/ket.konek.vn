"""Hình dạng response danh sách biên bản kiểm kê kho (lát 8D).

Các schema còn lại (`InventoryCountSheetIn`, `InventoryCountSheetOut`, `InventoryCountSheetDifference`…)
sống ở `modules/inventory/schemas.py` cùng chỗ với service — tệp này chỉ mang
thứ thuộc về **tầng HTTP**: bao phân trang.
"""

from __future__ import annotations

from pydantic import BaseModel

from ket.modules.inventory.schemas import InventoryCountSheetOut


class InventoryCountSheetListRow(InventoryCountSheetOut):
    """Một dòng danh sách — **không** mang dòng đếm.

    Biên bản của một kho 5.000 mã hàng có 5.000 dòng; nhét chúng vào mỗi phần
    tử của một trang 50 biên bản là một phản hồi hàng trăm nghìn đối tượng cho
    một màn hình chỉ vẽ ngày, số hiệu và trạng thái. Dòng đếm đọc qua
    `GET /inventory/count-sheets/{id}`.
    """

    lines: tuple[()] = ()


class InventoryCountSheetListResponse(BaseModel):
    items: tuple[InventoryCountSheetListRow, ...]
    total: int
    page: int
    page_size: int
