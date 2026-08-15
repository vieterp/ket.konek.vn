"""Migration và model phải mô tả **cùng một** schema.

Kiểu lỗi mà test này bắt: ai đó thêm cột vào model, chạy được ở máy mình (vì DB
cục bộ đã có cột đó từ lần thử trước) rồi commit mà quên sinh migration. Ở máy
khách hàng, cột không tồn tại và lỗi hiện ra ở một truy vấn hoàn toàn khác.

Cách kiểm: sau khi chạy migration lên một schema sạch, so sánh metadata của
model với schema thật. Còn khác biệt = còn thiếu migration.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from ket.kernel.datasets.provisioning import DatasetRef
from ket.model_registry import DatasetBase

pytestmark = pytest.mark.db


def test_no_pending_schema_changes(owner_engine: Engine, dataset_alpha: DatasetRef) -> None:
    with owner_engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{dataset_alpha.schema_name}"')
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True, "include_schemas": False},
        )
        diff = compare_metadata(context, DatasetBase.metadata)

    assert diff == [], (
        "Model và migration đã lệch nhau. Sinh migration mới:\n"
        "  uv run alembic -x schema=<schema> revision --autogenerate -m '<mô tả>'\n"
        f"Khác biệt: {diff}"
    )
