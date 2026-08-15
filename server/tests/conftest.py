"""Fixture dùng chung.

Phase 1 chưa chạm cơ sở dữ liệu. Fixture PostgreSQL (testcontainers) + fixture
dataset schema thêm ở phase 2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_ROOT = SERVER_ROOT / "src" / "konek"


@pytest.fixture(scope="session")
def domain_root() -> Path:
    """Thư mục gốc của mã nghiệp vụ (`server/src/konek`)."""
    return DOMAIN_ROOT
