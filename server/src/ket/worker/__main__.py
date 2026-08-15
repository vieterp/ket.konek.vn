"""Điểm vào tiến trình chạy tác vụ nền: `python -m ket.worker`.

Chạy như dịch vụ Windows / launchd cạnh app server (phase 11 đóng gói cả hai
trong một installer). Nhiều tiến trình chạy song song được — hàng đợi dùng
`FOR UPDATE SKIP LOCKED` nên hai worker không bao giờ giành cùng một job.

Cấu hình lấy từ cùng bộ `KET_*` với app server, nhưng **DSN khác**:
`KET_WORKER_DATABASE_URL` (vai trò `ket_worker`). Đó là điểm khác quan trọng
nhất giữa hai tiến trình — xem `settings.worker_database_url`.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sqlalchemy import Engine, create_engine

from ket.kernel.logging_setup import configure_logging, get_logger
from ket.kernel.persistence.session import create_session_factory
from ket.settings import Settings, get_settings
from ket.worker.runner import Worker

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Bộ phân tích tham số. Tách khỏi `main` để test gọi thẳng được."""
    parser = argparse.ArgumentParser(
        prog="python -m ket.worker",
        description="Tiến trình chạy tác vụ nền của Konek Két.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Chạy đúng một vòng qua mọi dữ liệu kế toán rồi thoát "
            "(dùng cho lịch OS hoặc để kiểm tra sau khi cài)"
        ),
    )
    return parser


def build_worker(settings: Settings) -> tuple[Worker, tuple[Engine, ...]]:
    """Dựng worker + danh sách engine người gọi phải `dispose()`.

    Trả engine ra ngoài thay vì giấu trong `Worker`: tiến trình dài ngày phải
    đóng pool sạch khi dừng, và một `Worker` tự quản vòng đời engine sẽ khiến
    test — nơi engine do fixture cấp — không tái dùng được lớp này.
    """
    worker_engine = create_engine(settings.worker_database_url)
    engines: list[Engine] = [worker_engine]

    owner_factory = None
    if settings.worker_owner_database_url is not None:
        # Chỉ dựng khi bản cài khai tường minh. Xem `JobPrivilege.CONTROL_OWNER`:
        # mặc định worker **không** cầm quyền owner, và đó là mặc định đúng.
        owner_engine = create_engine(settings.worker_owner_database_url)
        engines.append(owner_engine)
        owner_factory = create_session_factory(owner_engine)
        logger.warning(
            "worker_owner_connection_enabled",
            reason="tác vụ dọn phiên đăng nhập cần quyền ket_owner",
        )

    worker = Worker(
        settings,
        worker_factory=create_session_factory(worker_engine),
        owner_factory=owner_factory,
        engine=worker_engine,
    )
    return worker, tuple(engines)


def main(argv: Sequence[str] | None = None) -> int:
    """Điểm vào. Trả mã thoát 0 khi dừng theo yêu cầu."""
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=not settings.debug)

    worker, engines = build_worker(settings)
    try:
        if args.once:
            worker.run_once()
        else:
            worker.install_signal_handlers()
            worker.run_forever()
    finally:
        for engine in engines:
            engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover - điểm vào tiến trình
    raise SystemExit(main())
