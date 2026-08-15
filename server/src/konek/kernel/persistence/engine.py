"""Tạo `Engine` SQLAlchemy — một nơi duy nhất đặt tham số kết nối.

Ba điểm không được đổi mà không đọc lại ADR:

* **`NUMERIC` ↔ `Decimal`.** psycopg 3 trả `NUMERIC` thành `Decimal` mặc định.
  Không được cài adapter đổi nó sang dấu phẩy động "cho nhanh" — xem
  `konek.kernel.money`.
* **Hai vai trò DB tách biệt** (RT-02/RT-06): `konek_app` chạy runtime,
  `konek_owner` chỉ chạy DDL/migration. Cùng một hàm dựng engine, khác DSN.
  App server **không bao giờ** đăng nhập bằng superuser.
* **Không mở connection thứ hai trong một request.** ORM và SQL text dùng chung
  một `Session` (ADR-014, FR-NFR-003) — hai connection nghĩa là transaction
  không bao trọn thao tác và rollback chỉ lùi được một nửa.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from konek.settings import Settings

APPLICATION_NAME: Final[str] = "konek-app-server"
"""Hiện trong `pg_stat_activity` — thứ đầu tiên người trực nhìn khi DB nghẽn."""


def create_app_engine(settings: Settings, *, application_name: str = APPLICATION_NAME) -> Engine:
    """Engine runtime (vai trò `konek_app`).

    `pool_pre_ping` là bắt buộc với triển khai LAN: máy trạm ngủ, switch rớt,
    máy chủ DB khởi động lại — không có ping thì request đầu tiên sau đó chết
    bằng một traceback khó hiểu thay vì lặng lẽ mở lại connection.
    """
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={"application_name": application_name},
    )


def create_owner_engine(settings: Settings) -> Engine:
    """Engine đặc quyền (vai trò `konek_owner`) — chỉ dùng cho DDL/migration.

    `NullPool`: DDL chạy hiếm, và giữ sẵn connection đặc quyền trong pool là
    rủi ro không đổi lấy được gì.
    """
    return create_engine(
        settings.owner_database_url,
        poolclass=NullPool,
        connect_args={"application_name": f"{APPLICATION_NAME}-migrate"},
    )
