"""Kiểu giá trị đi qua ranh giới module.

Luật phụ thuộc #7 (plan.md §Bố cục, ADR-015) cấm `dict[str, Any]` đi qua ranh
giới module. Nhật ký lại phải mang giá trị của **bảng bất kỳ**, tức là dữ liệu
động thật sự. Cách giải ở đây: chuẩn hóa mọi giá trị về một tập nguyên thủy
JSON hẹp (`JsonPrimitive`) **ngay lúc ghi**, thay vì để `Any` chảy tiếp.

Hệ quả có chủ đích: `Decimal` và `datetime` được ghi vào nhật ký dưới dạng
chuỗi. Nhật ký để **đọc và đối chiếu**, không phải để tính toán lại — số để
tính luôn nằm ở bảng gốc.

Cột `JSONB` mang các kiểu này luôn khai kiểu SQL tường minh
(`mapped_column(JSONB, …)`), không để SQLAlchemy tự suy từ annotation.
"""

from __future__ import annotations

type JsonPrimitive = str | int | bool | None
"""Giá trị lá được phép nằm trong cột JSONB của nhật ký."""

type AuditValues = dict[str, JsonPrimitive]
"""Ảnh chụp "trước"/"sau" của một bản ghi: tên cột → giá trị đã chuẩn hóa."""
