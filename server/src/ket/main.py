"""FastAPI app factory.

Slice hiện tại của phase 2 dựng **nền dữ liệu và bảo mật**: engine + pool,
schema-per-dataset, vai trò DB tách đôi, RLS, nhật ký bất biến, phép tính tiền
`Decimal`. Auth/RBAC, idempotency, hàng đợi job, xử lý lỗi RFC 7807 và bắt tay
schema-version là các bước tiếp theo của cùng phase — `/health` vẫn là endpoint
duy nhất cho tới lúc đó.

Luồng nghiệp vụ đi qua REST + OpenAPI (LD-03). Client **không bao giờ** nối
thẳng PostgreSQL và **không** dùng API Tauri cho nghiệp vụ — giữ đường mở lên
chế độ trình duyệt trong LAN (topology, docs/system-architecture.md).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import Engine

from ket import __version__
from ket.kernel.datasets.bootstrap import verify_control_schema
from ket.kernel.datasets.provisioning import find_alembic_config, verify_dataset_schema_version
from ket.kernel.datasets.service import list_datasets
from ket.kernel.errors import UnsupportedPostgresVersionError
from ket.kernel.logging_setup import configure_logging, get_logger
from ket.kernel.persistence.engine import create_app_engine
from ket.kernel.persistence.session import create_session_factory
from ket.settings import Settings, get_settings

logger = get_logger(__name__)


class HealthResponse(BaseModel):
    """Trả lời của `/health`. Pydantic ở mọi ranh giới API (ADR-015)."""

    status: Literal["ok"]
    version: str
    deployment_mode: str


def verify_postgres_version(engine: Engine, settings: Settings) -> None:
    """Từ chối khởi động trên cụm PostgreSQL cũ hơn phiên bản đích (D4).

    Đọc `server_version_num` (số nguyên dạng `160004`) chứ không phân tích chuỗi
    `version()`: chuỗi đó khác nhau giữa bản Homebrew, Debian và container, và
    đã có tiền lệ dự án khác vấp vì so sánh chuỗi.
    """
    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SHOW server_version_num").scalar_one()
    major = int(raw) // 10000
    if major < settings.minimum_postgres_version:
        raise UnsupportedPostgresVersionError(
            "Cụm PostgreSQL cũ hơn phiên bản đích của bản cài",
            expected=settings.minimum_postgres_version,
            found=major,
        )


def verify_schema_versions(engine: Engine, settings: Settings) -> None:
    """Từ chối khởi động nếu DB lệch phiên bản với mã nguồn (LD-05, FR-NFR-054).

    Kiểm **từng** dataset chứ không chỉ một: mỗi schema có lịch sử migration
    riêng, và một dataset bị bỏ quên khi nâng cấp (đang khôi phục, tạm vô hiệu
    hóa) sẽ là dataset mà binary mới ghi sổ vào cấu trúc cũ. Thà không khởi
    động còn hơn ghi hỏng một bộ sổ.
    """
    verify_control_schema(engine)
    config = find_alembic_config(settings.alembic_ini_path)
    for dataset in list_datasets(engine, include_inactive=True):
        verify_dataset_schema_version(engine, dataset.schema_name, config)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Vòng đời tiến trình: dựng pool, kiểm phiên bản schema, dọn khi tắt."""
    settings: Settings = app.state.settings
    engine = create_app_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    if settings.verify_postgres_version_on_startup:
        verify_postgres_version(engine, settings)
        logger.info("postgres_version_verified")

    if settings.verify_schema_on_startup:
        verify_schema_versions(engine, settings)
        logger.info("schema_version_verified")

    try:
        yield
    finally:
        engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Dựng ứng dụng FastAPI.

    Truyền `settings` tường minh để test không phụ thuộc biến môi trường.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(level=resolved.log_level, json_output=not resolved.debug)

    app = FastAPI(
        title="Konek Két — App Server",
        version=__version__,
        lifespan=lifespan,
        debug=resolved.debug,
    )
    app.state.settings = resolved

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """Smoke check — không chạm cơ sở dữ liệu."""
        return HealthResponse(
            status="ok",
            version=__version__,
            deployment_mode=resolved.deployment_mode,
        )

    return app


app = create_app()
