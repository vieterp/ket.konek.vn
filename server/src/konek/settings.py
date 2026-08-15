"""Cấu hình app server.

Nguồn giá trị theo thứ tự ưu tiên: biến môi trường `KONEK_*` → file `.env` →
mặc định trong lớp này. **Bí mật thật (mật khẩu DB, khóa mã hóa backup, token
eSign) KHÔNG lấy từ đây ở môi trường thật** — chúng nằm trong OS keystore và
được nạp ở phase 2 (ADR-019 key-management). Phase 1 chỉ dựng khung cấu hình.
"""

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
    database_url: str = "postgresql+psycopg://localhost/konek"
    """DSN không kèm mật khẩu ở môi trường thật — mật khẩu nạp từ OS keystore.
    Kênh app→DB phải TLS verify-full khi DB khác host (ADR-019, RT-06)."""

    default_dataset_schema: str = "konek_default"
    """Một dữ liệu kế toán = một PG schema (ADR-017). Định tuyến schema theo
    dataset làm ở tầng session/connection — phase 2."""

    # --- Vận hành
    debug: bool = False
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


def get_settings() -> Settings:
    """Đọc cấu hình từ môi trường.

    Phase 2 sẽ bọc bằng cache theo tiến trình; phase 1 giữ đơn giản.
    """
    return Settings()
