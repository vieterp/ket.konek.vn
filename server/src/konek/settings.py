"""Cấu hình app server.

Nguồn giá trị theo thứ tự ưu tiên: biến môi trường `KONEK_*` → file `.env` →
mặc định trong lớp này. **Bí mật thật (mật khẩu DB, khóa mã hóa backup, token
eSign) KHÔNG lấy từ đây ở môi trường thật** — chúng nằm trong OS keystore và
được nạp ở phase 2 (ADR-019 key-management). Phase 1 chỉ dựng khung cấu hình.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DeploymentMode = Literal["standalone", "lan"]


class Settings(BaseSettings):
    """Cấu hình runtime của một tiến trình app server."""

    model_config = SettingsConfigDict(
        env_prefix="KONEK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # --- Triển khai (LD-01: một-máy và LAN là cấu hình, không phải nhánh code)
    deployment_mode: DeploymentMode = "standalone"
    """`standalone` = app server + PostgreSQL + client cùng PC, bind 127.0.0.1.
    `lan` = app server chạy như dịch vụ trên máy host, 5–50 client."""

    host: str = "127.0.0.1"
    port: int = 5443

    # --- Cơ sở dữ liệu
    database_url: str = "postgresql+psycopg://konek_app@localhost/konek"
    """DSN **runtime**, đăng nhập bằng vai trò `konek_app`: không sở hữu bảng,
    không `BYPASSRLS`, không sửa được `audit_log` (RT-02/RT-04). Không kèm mật
    khẩu ở môi trường thật — mật khẩu nạp từ OS keystore. Kênh app→DB phải TLS
    verify-full khi DB khác host (ADR-019, RT-06)."""

    owner_database_url: str = "postgresql+psycopg://konek_owner@localhost/konek"
    """DSN **đặc quyền** cho DDL/migration/tạo dataset. Tách hẳn khỏi
    `database_url` là điều kiện để nhật ký bất biến và RLS có hiệu lực — dùng
    chung một vai trò cho cả hai việc thì cả hai cơ chế đều vô hiệu."""

    db_pool_size: int = 10
    db_max_overflow: int = 5
    """Quy mô mục tiêu đã chốt (OQ#3): ~20 người dùng đồng thời. Job nặng chạy ở
    tiến trình worker riêng nên không ăn vào pool của API."""

    default_dataset_schema: str = "konek_default"
    """Một dữ liệu kế toán = một PG schema (ADR-017). Chỉ là mặc định cho lệnh
    migration chạy tay; runtime lấy schema từ bảng `datasets` theo dataset của
    request."""

    alembic_ini_path: Path | None = None
    """Ghi đè đường dẫn `alembic.ini` khi chạy từ thư mục khác hoặc từ bản đóng
    gói (S4/phase 11). `None` = suy từ vị trí gói."""

    verify_schema_on_startup: bool = True
    """Chặn khởi động khi schema DB lệch phiên bản migration (LD-05,
    FR-NFR-054). Chỉ đặt `False` cho test/CI không có PostgreSQL."""

    # --- Vận hành
    debug: bool = False
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


def get_settings() -> Settings:
    """Đọc cấu hình từ môi trường.

    Phase 2 sẽ bọc bằng cache theo tiến trình; phase 1 giữ đơn giản.
    """
    return Settings()
