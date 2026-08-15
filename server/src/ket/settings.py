"""Cấu hình app server.

Nguồn giá trị theo thứ tự ưu tiên: biến môi trường `KET_*` → file `.env` →
mặc định trong lớp này. **Bí mật thật (mật khẩu DB, khóa mã hóa backup, token
eSign) KHÔNG lấy từ đây ở môi trường thật** — chúng nằm trong OS keystore và
được nạp ở phase 2 (ADR-019 key-management). Phase 1 chỉ dựng khung cấu hình.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DeploymentMode = Literal["standalone", "lan"]


class Settings(BaseSettings):
    """Cấu hình runtime của một tiến trình app server."""

    model_config = SettingsConfigDict(
        env_prefix="KET_",
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
    database_url: str = "postgresql+psycopg://ket_app@localhost/ket"
    """DSN **runtime**, đăng nhập bằng vai trò `ket_app`: không sở hữu bảng,
    không `BYPASSRLS`, không sửa được `audit_log` (RT-02/RT-04). Không kèm mật
    khẩu ở môi trường thật — mật khẩu nạp từ OS keystore. Kênh app→DB phải TLS
    verify-full khi DB khác host (ADR-019, RT-06)."""

    owner_database_url: str = "postgresql+psycopg://ket_owner@localhost/ket"
    """DSN **đặc quyền** cho DDL/migration/tạo dataset. Tách hẳn khỏi
    `database_url` là điều kiện để nhật ký bất biến và RLS có hiệu lực — dùng
    chung một vai trò cho cả hai việc thì cả hai cơ chế đều vô hiệu."""

    db_pool_size: int = 10
    db_max_overflow: int = 5
    """Quy mô mục tiêu đã chốt (OQ#3): ~20 người dùng đồng thời. Job nặng chạy ở
    tiến trình worker riêng nên không ăn vào pool của API."""

    default_dataset_schema: str = "ket_default"
    """Một dữ liệu kế toán = một PG schema (ADR-017). Chỉ là mặc định cho lệnh
    migration chạy tay; runtime lấy schema từ bảng `datasets` theo dataset của
    request."""

    alembic_ini_path: Path | None = None
    """Ghi đè đường dẫn `alembic.ini` khi chạy từ thư mục khác hoặc từ bản đóng
    gói (S4/phase 11). `None` = suy từ vị trí gói."""

    minimum_postgres_version: int = 16
    """Phiên bản PostgreSQL tối thiểu của bản cài (quyết định D4 của phase 2).

    Ghim để dev, CI và máy khách chạy **cùng một** bộ hành vi: RLS, quy tắc
    `CREATEROLE`, `MERGE`, `NULLS NOT DISTINCT` đều khác nhau giữa các phiên
    bản, và đây là những thứ cơ chế cô lập dữ liệu dựa vào. Kiểm lúc khởi động
    thay vì để một câu lệnh của phase 3 nổ giữa kỳ khóa sổ ở nơi cài đặt."""

    verify_postgres_version_on_startup: bool = True
    """Bật cổng `minimum_postgres_version` lúc khởi động.

    Cờ **riêng**, không gộp vào `verify_schema_on_startup`: hai cổng canh hai thứ
    khác nhau — phiên bản của **cụm** so với phiên bản của **schema**. Gộp lại thì
    một bản cài tắt kiểm schema (đang khôi phục, đang gỡ rối) cũng tắt luôn cổng
    phiên bản cụm mà không ai chủ ý làm thế."""

    verify_schema_on_startup: bool = True
    """Chặn khởi động khi schema DB lệch phiên bản migration (LD-05,
    FR-NFR-054). Chỉ đặt `False` cho test/CI không có PostgreSQL."""

    # --- Danh tính & bí mật (ADR-019, RT-05)
    keyring_service: str = "ket"
    """Tên dịch vụ trong OS keystore. Đổi khi cần chạy hai bản cài trên cùng một
    máy (dev + demo) mà không để chúng dùng chung khóa mã hóa."""

    app_key: SecretStr | None = None
    """Khóa Fernet dạng rõ, **ghi đè** OS keystore. Chỉ dùng cho test/CI và bản
    cài container — nơi keystore không có, hoặc thuộc về người dùng khác với
    người chạy dịch vụ. Trên máy khách thật, để trống và dùng
    `python -m ket.admin generate-app-key`."""

    session_ttl_minutes: int = 720
    """Tuổi thọ tuyệt đối của một phiên đăng nhập (12 giờ).

    Phủ trọn một ngày làm việc kể cả ca dài, nên người dùng bình thường không
    bao giờ bị đá ra giữa chừng; máy trạm bỏ quên qua đêm thì sáng hôm sau phải
    đăng nhập lại. Không có gia hạn trượt: gia hạn theo hoạt động nghĩa là một
    phiên bị chiếm sẽ tự nuôi mình sống mãi."""

    # --- Vận hành
    debug: bool = False
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


def get_settings() -> Settings:
    """Đọc cấu hình từ môi trường.

    Phase 2 sẽ bọc bằng cache theo tiến trình; phase 1 giữ đơn giản.
    """
    return Settings()
