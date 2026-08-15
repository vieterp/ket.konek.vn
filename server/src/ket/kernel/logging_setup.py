"""Log có cấu trúc, ghi ra tệp — công cụ chẩn đoán duy nhất ở nơi cài đặt.

Bối cảnh quyết định thiết kế: bản cài chạy trong mạng nội bộ của khách hàng,
không có hệ thống thu thập log tập trung, và người báo lỗi là kế toán chứ không
phải quản trị hệ thống (LD-01). Vì vậy:

* **JSON theo dòng**, xoay vòng theo dung lượng → gửi kèm được khi báo lỗi và
  đọc bằng máy được khi phân tích.
* **`correlation_id` trên mọi dòng** → nối được log với `audit_log.correlation_id`,
  tức là dựng lại được "người dùng bấm nút gì thì hệ thống làm gì".
* **Không bao giờ log bí mật.** Mật khẩu, PIN, bí mật TOTP, chuỗi kết nối có
  credential không được xuất hiện — log đi ra ngoài dễ hơn database rất nhiều.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog

MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def configure_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    json_output: bool = True,
) -> None:
    """Dựng structlog + logging chuẩn. Chạy một lần lúc khởi động tiến trình.

    Log của thư viện (SQLAlchemy, uvicorn, alembic) đi qua cùng đường ra để
    không có dòng nào lạc sang chỗ khác — khi cần dựng lại một sự cố, thiếu
    dòng còn tệ hơn thừa dòng.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
        )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(message)s",
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Logger đã gắn tên module."""
    return structlog.stdlib.get_logger(name)
