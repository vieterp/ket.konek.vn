"""Chiều phân tích dựng sẵn, gieo lúc cấp dữ liệu kế toán (FR-SYS-051).

Chỉ **một** chiều được dựng sẵn: "Mã thống kê". SRS §7 mô tả nó là "chiều phân
tích tự do trên báo cáo" — nghĩa là một chiều mà mỗi doanh nghiệp tự đổ giá trị
vào, chứ không phải một khái niệm kế toán có sẵn nội dung. Vì thế ở đây gieo
**định nghĩa** chiều, không gieo giá trị nào: đoán hộ khách hàng "miền Bắc /
miền Nam" là đoán sai với doanh nghiệp phân theo kênh bán.

Đây cũng là bằng chứng cho tiêu chí *"chiều mở rộng khai bằng cấu hình, không
sửa code"*: chiều dựng sẵn duy nhất của hệ thống cũng chỉ là một dòng dữ liệu,
đi qua đúng bộ luật (`validate_value_source`) mà đường API dùng. Không có đặc
quyền nào dành riêng cho chiều "của hệ thống", nên cũng không có tính năng nào
chỉ chiều dựng sẵn mới có.

**Vì sao Core `insert` chứ không `DimensionService.declare`:** lúc cấp dữ liệu
kế toán chưa có người thực hiện nào, mà `AnalysisDimension` là bảng có ghi nhật
ký — một lần ghi qua ORM Session sẽ đâm vào `AuditContextMissingError`, đúng như
thiết kế fail-closed của listener. Cùng lý do `role_service` gieo `roles` bằng
Core. Phần **luật** không đi theo đường ghi: nó nằm ở `validate_value_source`,
được gọi ở cả hai đường.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Connection, insert, select

from ket.kernel.dimensions.models import AnalysisDimension, DimensionValueSource
from ket.kernel.dimensions.service import validate_value_source
from ket.kernel.persistence.seeding import bind_seed_schema

STATISTICAL_CODE_DIMENSION: Final[str] = "STAT"
"""Mã của chiều "Mã thống kê" (FR-SYS-051).

Hằng chứ không chuỗi rải rác: gói cấu hình phase 5 và mẫu báo cáo đều tham
chiếu chiều này theo mã, nên nó là một **hợp đồng**, không phải dữ liệu mẫu."""

_BUILTIN_NAME: Final[str] = "Mã thống kê"
_BUILTIN_NAME_EN: Final[str] = "Statistical code"


def ensure_builtin_dimensions(connection: Connection, schema: str) -> int:
    """Gieo chiều dựng sẵn vào một schema dataset. Trả số chiều thêm mới.

    Nhận `Connection` chứ không nhà máy `Session` vì nó chạy trong cùng mạch
    `owner_engine.begin()` với `ensure_admin_role` lúc cấp dataset — hai bước
    gieo phải hoặc cùng xong hoặc cùng không, và một dữ liệu kế toán có vai trò
    nhưng thiếu chiều thống kê là trạng thái mà không lệnh nào sửa về sau.

    Thêm-nếu-thiếu chứ không ghi đè: khách hàng **được** đổi tên hiển thị của
    chiều này (nhiều nơi gọi nó là "mã phân tích"), và một lần nâng cấp không
    được lặng lẽ đặt lại tên đó.
    """
    bind_seed_schema(connection, schema)

    source = DimensionValueSource.LIST
    validate_value_source(source, None)

    existing = connection.scalar(
        select(AnalysisDimension.id).where(AnalysisDimension.code == STATISTICAL_CODE_DIMENSION)
    )
    if existing is not None:
        return 0

    connection.execute(
        insert(AnalysisDimension).values(
            code=STATISTICAL_CODE_DIMENSION,
            name=_BUILTIN_NAME,
            name_en=_BUILTIN_NAME_EN,
            value_source=source.value,
            master_slug=None,
            is_required=False,
            applies_to_accounts=None,
            is_active=True,
            row_version=1,
        )
    )
    return 1
