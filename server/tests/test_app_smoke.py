"""Smoke test cho khung app — chứng minh khung dựng và chạy được.

Phase 1 chưa có nghiệp vụ nào để kiểm; mục đích duy nhất là CI có tín hiệu
"khung còn sống" trước khi phase 2 gắn auth/RBAC/DB vào.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ket.main import create_app
from ket.settings import Settings


def test_health_endpoint_returns_ok() -> None:
    app = create_app(Settings(deployment_mode="standalone", verify_schema_on_startup=False))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["deployment_mode"] == "standalone"


def test_openapi_schema_generates() -> None:
    """Hợp đồng client↔server sinh từ OpenAPI (LD-03) — schema phải dựng được."""
    app = create_app(Settings(verify_schema_on_startup=False))

    schema = app.openapi()

    assert schema["info"]["title"] == "Konek Két — App Server"
    assert "/health" in schema["paths"]
