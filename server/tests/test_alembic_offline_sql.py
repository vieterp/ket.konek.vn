"""Chế độ offline của Alembic (`upgrade --sql`) phải chạy được, không cần DB.

Vì sao đây là hợp đồng chứ không phải tiện ích: DBA của khách hàng duyệt SQL
trước khi cho chạy lên cụm sản xuất, và `migrations/env.py` cài đặt chế độ này
tường minh (`run_migrations_offline`). Một migration lỡ tay hỏi DB — ví dụ
`op.get_bind().exec_driver_sql("SELECT current_schema()")` — sẽ làm vỡ chế độ đó
với `AttributeError: 'MockConnection' object has no attribute 'exec_driver_sql'`,
và không cổng nào khác bắt được vì mọi test khác đều có DB thật.

Test này không cần PostgreSQL (không đánh dấu `db`) nên nó chạy trên cả ba hệ
điều hành trong CI.
"""

from __future__ import annotations

import pytest
from alembic import command

from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE, find_alembic_config

OFFLINE_SCHEMA = "ds_offline_probe"


def test_upgrade_sql_emits_grants_for_the_target_dataset_role(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sinh SQL cho một schema và khẳng định quyền cấp cho **vai trò của schema đó**."""
    config = find_alembic_config()
    config.attributes[ALEMBIC_SCHEMA_ATTRIBUTE] = OFFLINE_SCHEMA
    try:
        command.upgrade(config, "head", sql=True)
    finally:
        config.attributes.pop(ALEMBIC_SCHEMA_ATTRIBUTE, None)

    sql = capsys.readouterr().out

    assert "CREATE TABLE audit_log" in sql
    # Bên nhận quyền suy từ schema đích, không phải hằng số `ket_app`.
    assert f"GRANT INSERT, SELECT ON audit_log TO {OFFLINE_SCHEMA}_app;" in sql
    assert "TO ket_app;" not in sql
    # Kết thúc bình thường: có `COMMIT` nghĩa là chạy hết migration, không dừng
    # giữa chừng vì ngoại lệ.
    assert "COMMIT;" in sql
