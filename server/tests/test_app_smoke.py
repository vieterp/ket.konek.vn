"""Smoke test cho khung app — chứng minh khung dựng và chạy được.

Hai cờ kiểm lúc khởi động đều tắt: các test này chạy trong nhóm không cần DB
(mọi hệ điều hành trong CI), mà cả hai cổng đều phải mở connection thật.

Phase 1 chưa có nghiệp vụ nào để kiểm; mục đích duy nhất là CI có tín hiệu
"khung còn sống" trước khi phase 2 gắn auth/RBAC/DB vào.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ket.main import create_app
from ket.settings import Settings


def test_health_endpoint_returns_ok() -> None:
    app = create_app(
        Settings(
            deployment_mode="standalone",
            verify_schema_on_startup=False,
            verify_postgres_version_on_startup=False,
        )
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["deployment_mode"] == "standalone"


def test_openapi_schema_generates() -> None:
    """Hợp đồng client↔server sinh từ OpenAPI (LD-03) — schema phải dựng được."""
    app = create_app(
        Settings(verify_schema_on_startup=False, verify_postgres_version_on_startup=False)
    )

    schema = app.openapi()

    assert schema["info"]["title"] == "Konek Két — App Server"
    assert "/health" in schema["paths"]


def test_startup_gates_default_to_enabled() -> None:
    """Hai cổng kiểm lúc khởi động phải **mặc định bật**.

    Test khác tự đặt cờ = True rồi khẳng định cổng chặn — nó chứng minh cổng chạy
    khi được bật, không chứng minh bản cài mặc định có bật. Đổi mặc định thành
    `False` là vô hiệu hóa cả hai cổng trên mọi bản cài, và trước dòng này thì
    không có gì đỏ.
    """
    settings = Settings()

    assert settings.verify_postgres_version_on_startup is True
    assert settings.verify_schema_on_startup is True
    assert settings.minimum_postgres_version == 16
