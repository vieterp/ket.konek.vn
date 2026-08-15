"""FastAPI app factory.

Phase 1 cố ý chỉ có smoke endpoint `/health` để chứng minh khung chạy được và
CI có thứ để kiểm. Auth/RBAC, RLS, định tuyến schema dataset, audit, bắt tay
schema-version, xử lý lỗi RFC 7807 — tất cả thuộc phase 2.

Luồng nghiệp vụ đi qua REST + OpenAPI (LD-03). Client **không bao giờ** nối
thẳng PostgreSQL và **không** dùng API Tauri cho nghiệp vụ — giữ đường mở lên
chế độ trình duyệt trong LAN (topology, docs/system-architecture.md).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from konek import __version__
from konek.settings import Settings, get_settings


class HealthResponse(BaseModel):
    """Trả lời của `/health`. Pydantic ở mọi ranh giới API (ADR-015)."""

    status: Literal["ok"]
    version: str
    deployment_mode: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Vòng đời tiến trình.

    Phase 2 gắn vào đây: engine SQLAlchemy, đăng ký Protocol của kernel, khởi
    động reaper của hàng đợi job (ADR-014).
    """
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Dựng ứng dụng FastAPI.

    Truyền `settings` tường minh để test không phụ thuộc biến môi trường.
    """
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="Konek Accounting App Server",
        version=__version__,
        lifespan=lifespan,
        debug=resolved.debug,
    )

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
