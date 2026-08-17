"""Lấy trước khóa chính từ sequence của bảng.

Cần đến vì hai bảng cây (`branches` và mọi danh mục) lưu **materialized path**,
mà path chứa chính khóa chính của dòng đó. Không có id trước thì chỉ còn đường
"chèn với path tạm rồi `UPDATE` lại" — và bản ghi mới toanh sẽ mang
`row_version = 2` cùng một dòng nhật ký "vừa tạo xong đã sửa", tức là hai thứ
nhiễu mà kiểm toán viên phải tự loại suốt vòng đời sản phẩm.

Số lấy ra **không** trả lại khi transaction rollback: sequence của PostgreSQL cố
ý nằm ngoài transaction để hai người chèn đồng thời không phải chờ nhau. Với
khóa nội bộ thì đứt quãng vô hại — không ai nhìn thấy nó. Chỗ **không** được
đứt quãng là số chứng từ, và nó có cơ chế riêng (`kernel/numbering/service.py`).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.security.grants import serial_sequence_name


def reserve_id(session: Session, table: str) -> int:
    """Giá trị kế tiếp của sequence `<table>_id_seq`.

    Tên sequence đi vào câu lệnh dưới dạng **tham số ràng buộc** chứ không ghép
    chuỗi: `nextval(text)` nhận tên ở dạng dữ liệu, nên đây là một trong số ít
    chỗ chạm tới đối tượng DB theo tên mà vẫn giữ được extended protocol (xem
    `tests/test_no_sql_string_interpolation.py`).
    """
    value = session.execute(select(func.nextval(serial_sequence_name(table)))).scalar_one()
    return int(value)
