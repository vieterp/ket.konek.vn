"""Hai gốc khai báo ORM — schema điều khiển và schema dataset (ADR-017).

Vì sao **hai** `MetaData` chứ không phải một:

* `ControlBase` gắn cứng `schema="public"`: `datasets`, `users` toàn cục,
  `system_metadata`. Chỉ có **một** bản của các bảng này trong cả DB.
* `DatasetBase` **không** khai schema: bảng nghiệp vụ tồn tại **một bản cho mỗi
  dataset** và được định tuyến bằng `search_path` lúc chạy (xem
  `persistence/session.py`). Nếu gắn cứng schema ở đây thì một binary chỉ nói
  chuyện được với đúng một dataset.

`DatasetBase.metadata` cũng chính là `target_metadata` của Alembic — migration
chạy lặp cho từng schema dataset.

**Không có khóa ngoại chéo schema.** Bảng trong dataset chỉ tham chiếu bảng
cùng dataset; liên hệ tới `public.users` lưu bằng `user_id` trần, kiểm ở tầng
ứng dụng. Lý do là vận hành: sao lưu/khôi phục làm **theo từng schema dataset**
(plan.md §Cross-cutting, RT-03/RT-14) — một bản dump có FK trỏ ra ngoài schema
sẽ không restore được sang cụm khác, đúng vào lúc cần nó nhất.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from ket.kernel.datasets.naming import CONTROL_SCHEMA

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}
"""Tên ràng buộc sinh theo quy ước để Alembic autogenerate ổn định giữa các lần
chạy — không có quy ước này thì PostgreSQL tự đặt tên và mọi migration sinh ra
đều khác nhau, khiến diff schema vô dụng."""


class ControlBase(DeclarativeBase):
    """Gốc của bảng trong schema điều khiển (`public`)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION, schema=CONTROL_SCHEMA)


class DatasetBase(DeclarativeBase):
    """Gốc của bảng nghiệp vụ — một bản trong mỗi schema dataset."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
