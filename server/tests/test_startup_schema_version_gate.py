"""App server phải khởi động được **sau khi** đã có dữ liệu kế toán (LD-05).

Lỗi mà bộ test này khóa lại: `verify_schema_versions` đọc `alembic_version` của
từng dataset bằng vai trò runtime, nhưng bảng đó do Alembic tạo và không
migration nào cấp quyền cho nó. Bản cài mới chạy tốt (vòng lặp dataset rỗng) rồi
chết ngay khi khách hàng tạo dữ liệu kế toán đầu tiên — tức là hỏng ở nơi cài
đặt, không hỏng trên máy lập trình.

CI không bắt được vì hai test smoke duy nhất dựng app đều tắt cờ kiểm. Ở đây
kiểm được bật, và kiểm trên một dataset đã provision thật.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from konek.kernel.datasets.bootstrap import CONTROL_SCHEMA_VERSION, verify_control_schema
from konek.kernel.datasets.provisioning import DatasetRef, verify_dataset_schema_version
from konek.kernel.errors import SchemaVersionMismatchError
from konek.main import create_app, verify_schema_versions
from konek.settings import Settings

pytestmark = pytest.mark.db


def test_app_starts_with_verification_enabled_and_a_provisioned_dataset(
    test_settings: Settings, dataset_alpha: DatasetRef
) -> None:
    """Đường khởi động thật: cờ kiểm bật, dataset đã tồn tại."""
    settings = test_settings.model_copy(update={"verify_schema_on_startup": True})

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_runtime_role_can_read_dataset_revision(
    app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Vai trò runtime phải ĐỌC được `alembic_version` — handshake (bước 19) cũng cần."""
    verify_dataset_schema_version(app_engine, dataset_alpha.schema_name)


def test_runtime_role_cannot_write_dataset_revision(
    app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """…nhưng chỉ đọc: ghi phiên bản schema là việc của `konek_owner`."""
    from sqlalchemy.exc import ProgrammingError

    with app_engine.connect() as connection, pytest.raises(ProgrammingError, match="permission"):
        connection.execute(
            text(f"UPDATE \"{dataset_alpha.schema_name}\".alembic_version SET version_num = '0666'")
        )


def test_version_mismatch_refuses_startup(
    app_engine: Engine, owner_engine: Engine, dataset_alpha: DatasetRef, test_settings: Settings
) -> None:
    """Lệch revision → từ chối khởi động, không ghi sổ vào cấu trúc sai."""
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        original = connection.execute(
            text(f'SELECT version_num FROM "{schema}".alembic_version')
        ).scalar_one()
        connection.execute(text(f"UPDATE \"{schema}\".alembic_version SET version_num = '0666'"))
    try:
        with pytest.raises(SchemaVersionMismatchError):
            verify_schema_versions(app_engine, test_settings)
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text(f'UPDATE "{schema}".alembic_version SET version_num = :v'), {"v": original}
            )


def test_control_schema_version_is_a_real_gate(app_engine: Engine, owner_engine: Engine) -> None:
    """Phiên bản schema điều khiển phải **chặn được**, không chỉ để trưng bày.

    Trước khi sửa, `ensure_control_schema` lặng lẽ dán nhãn phiên bản mới lên
    một DB cũ, nên `verify_control_schema` không bao giờ hỏng được — số phiên
    bản trở thành trang trí.
    """
    verify_control_schema(app_engine)

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE public.system_metadata SET value = '999' WHERE key = 'control_schema_version'"
            )
        )
    try:
        with pytest.raises(SchemaVersionMismatchError):
            verify_control_schema(app_engine)
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.system_metadata SET value = :v "
                    "WHERE key = 'control_schema_version'"
                ),
                {"v": CONTROL_SCHEMA_VERSION},
            )
