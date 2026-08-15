"""Không được che bảng thật bằng bảng tạm (`pg_temp`).

Đường tấn công mà bộ test này khóa lại: nhật ký bất biến chống được
`UPDATE`/`DELETE`/`TRUNCATE`/`DROP` — nhưng kẻ tấn công **không cần** xóa gì.
Khi `search_path` không nêu `pg_temp`, PostgreSQL tìm nó **trước tiên**, nên chỉ
cần `CREATE TEMP TABLE audit_log (…)` là mọi dòng nhật ký sau đó rơi vào bảng
tạm và bốc hơi khi phiên kết thúc, trong khi ghi nghiệp vụ vẫn vào bảng thật.

Hai lớp phòng thủ, kiểm cả hai: `search_path` nêu `pg_temp` ở **cuối**, và vai
trò runtime bị thu hồi quyền `TEMPORARY`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from konek.kernel.datasets.provisioning import DatasetRef
from konek.kernel.security.rls import set_search_path_statement

pytestmark = pytest.mark.db

_TEMP_AUDIT_DDL = """
CREATE TEMP TABLE audit_log (
    id bigserial PRIMARY KEY, occurred_at timestamptz DEFAULT now(),
    user_id int, branch_id int, entity_type text, entity_id text,
    action text, old_values jsonb, new_values jsonb,
    correlation_id uuid, client_info text
)
"""


def test_search_path_names_pg_temp_last() -> None:
    """Lớp phòng thủ một: nêu tường minh nên `pg_temp` không còn được tìm trước."""
    statement = set_search_path_statement("ds_alpha")
    assert statement.endswith(", public, pg_temp")


def test_runtime_role_cannot_create_temp_tables(app_engine: Engine) -> None:
    """Lớp phòng thủ hai: `REVOKE TEMPORARY` — không tạo được bảng tạm thì không che được gì."""
    with app_engine.connect() as connection, pytest.raises(ProgrammingError, match="permission"):
        connection.execute(text(_TEMP_AUDIT_DDL))


def test_owner_role_cannot_create_temp_tables_either(owner_engine: Engine) -> None:
    """`REVOKE TEMPORARY … FROM PUBLIC` chặn cả vai trò DDL, không chỉ runtime.

    `konek_owner` không phải superuser nên nó cũng mất quyền này — đúng ý đồ:
    không tiến trình nào của ứng dụng cần bảng tạm.
    """
    with owner_engine.connect() as connection, pytest.raises(ProgrammingError, match="permission"):
        connection.execute(text(_TEMP_AUDIT_DDL))


def test_writes_land_in_the_real_audit_table_even_if_a_temp_one_exists(
    superuser_engine: Engine, app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Kiểm lớp phòng thủ MỘT một cách độc lập với lớp hai.

    Phải dùng superuser để dựng bảng che, vì lớp hai đã chặn mọi vai trò của
    ứng dụng. Kiểm riêng như vậy mới biết `search_path` tự nó đã đúng — nếu chỉ
    dựa vào `REVOKE`, thì ngày ai đó cấp lại quyền `TEMPORARY` (một extension,
    một công cụ báo cáo) là lỗ hổng quay lại mà không test nào đỏ.
    """
    with superuser_engine.connect() as connection:
        connection.execute(text(_TEMP_AUDIT_DDL))
        connection.execute(text(set_search_path_statement(dataset_alpha.schema_name)))
        connection.execute(
            text(
                "INSERT INTO audit_log (user_id, entity_type, entity_id, action) "
                "VALUES (9, 'shadow-probe', '1', 'created')"
            )
        )
        connection.commit()

    with app_engine.connect() as connection:
        landed = connection.execute(
            text(
                f'SELECT count(*) FROM "{dataset_alpha.schema_name}".audit_log '
                "WHERE entity_type = 'shadow-probe'"
            )
        ).scalar_one()
    assert landed == 1
