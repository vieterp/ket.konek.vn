"""Đọc tên ràng buộc từ một `IntegrityError` của psycopg.

Tồn tại vì luật đúng là "ràng buộc thật nằm ở DB, tầng service chỉ dịch lỗi"
(doctrine từ 6C H-1/6D H-2): kiểm-trước-rồi-ghi thua một lượt ghi song song mọi
lần. Nhưng để dịch được, nơi bắt lỗi phải biết ràng buộc NÀO vừa vỡ — và đó là
một phép đọc thuộc tính driver, không phải nghiệp vụ của module nào.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

__all__ = ["violated_constraint"]


def violated_constraint(error: IntegrityError) -> str | None:
    """Tên ràng buộc bị vi phạm, `None` khi driver không nói (không phải psycopg,
    hoặc lỗi không mang `diag`)."""
    diagnostic = getattr(error.orig, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    return name if isinstance(name, str) else None
