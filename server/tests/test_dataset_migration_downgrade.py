"""`downgrade()` của migration dataset phải chạy được — và chạy **lên lại** được.

Cổng bổ sung sau review 3B-1 (M-8). Trước đó không gì canh `downgrade()`: nó chỉ
được viết ra rồi để đấy, và một dòng `op.drop_table` sai thứ tự khóa ngoại chỉ lộ
ra đúng lúc có người cần nó nhất — giữa một lần nâng cấp hỏng ở máy khách hàng.

Vì sao `downgrade` đáng một cổng dù không ai chạy nó hằng ngày: nó là đường lùi
của một bản cài đang chạy. Chỗ để phát hiện nó hỏng là ở đây, không phải ở lúc
DBA của khách hàng đã dừng dịch vụ và đang đếm phút.

Chạy trên một dataset **dùng một lần** chứ không dùng `dataset_alpha`: hạ rồi
nâng lại một schema mà bộ test khác đang dùng là cách xóa dữ liệu ngay dưới chân
chúng.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from alembic import command
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.currency.models import Currency
from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import (
    ALEMBIC_SCHEMA_ATTRIBUTE,
    DatasetRef,
    current_revision,
    drop_dataset_schema,
    find_alembic_config,
    provision_dataset,
    upgrade_dataset_schema,
)
from ket.kernel.periods.models import (
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

DATASET_CODE = "downgrade_probe"

CATALOG_TABLES = (
    "projects",
    "warehouses",
    "payment_terms",
    "banks",
    "analysis_dimensions",
    "analysis_dimension_values",
)
"""Mẫu đại diện cho `0003` — đủ để thấy bảng biến mất rồi quay lại."""


@pytest.fixture
def probe_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    dataset = provision_dataset(
        owner_engine, code=DATASET_CODE, name="Dữ liệu thử hạ cấp", scheme="TT99"
    )
    try:
        yield dataset
    finally:
        drop_dataset_schema(owner_engine, DATASET_CODE)


def _tables(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                {"schema": schema},
            ).all()
        )


def _downgrade(engine: Engine, schema: str, revision: str) -> None:
    """Hạ một schema dataset xuống một revision cụ thể.

    Truyền sẵn `connection` như `upgrade_dataset_schema` làm: `migrations/env.py`
    lấy connection từ `config.attributes` chứ không từ `sqlalchemy.url` (thông
    tin đăng nhập không nằm trong repo). Thiếu nó, Alembic tự mở connection
    riêng và không có `search_path` nào được đặt — `no schema has been selected
    to create in`.
    """
    config = find_alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        config.attributes[ALEMBIC_SCHEMA_ATTRIBUTE] = schema
        try:
            command.downgrade(config, revision)
        finally:
            config.attributes.pop("connection", None)
            config.attributes.pop(ALEMBIC_SCHEMA_ATTRIBUTE, None)


def test_downgrade_then_upgrade_restores_every_table(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """`0003` hạ xuống `0002` rồi nâng lại phải cho ra **đúng** bộ bảng ban đầu.

    Khẳng định cả hai chiều: hạ thật sự bỏ bảng (nếu không, `downgrade` là hàm
    rỗng và cổng này vô nghĩa), và nâng lại dựng đủ.
    """
    schema = probe_dataset.schema_name
    before = _tables(owner_engine, schema)
    assert set(CATALOG_TABLES) <= before, "migration 0003 chưa chạy trên dataset thử"

    _downgrade(owner_engine, schema, "0002")
    after_downgrade = _tables(owner_engine, schema)
    assert not (set(CATALOG_TABLES) & after_downgrade), (
        "downgrade() của 0003 không bỏ bảng nào — nó là hàm rỗng, "
        f"còn lại: {sorted(set(CATALOG_TABLES) & after_downgrade)}"
    )

    upgrade_dataset_schema(owner_engine, schema)
    assert _tables(owner_engine, schema) == before


def test_the_dataset_role_still_writes_after_a_downgrade_round_trip(
    owner_engine: Engine, app_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """Nâng lại phải cấp lại **quyền**, không chỉ dựng lại bảng.

    `DROP TABLE` cuốn theo mọi `GRANT` trên bảng đó. Nếu `_apply_grants()` không
    chạy lại ở lượt nâng thứ hai, bảng có mặt nhưng vai trò runtime không ghi
    được — và triệu chứng (`permission denied`) không hề gợi tới lần hạ cấp đã
    xảy ra từ trước đó.
    """
    schema = probe_dataset.schema_name
    _downgrade(owner_engine, schema, "0002")
    upgrade_dataset_schema(owner_engine, schema)

    with app_engine.begin() as connection:
        # `SET ROLE` trước `search_path`: quyền được cấp cho vai trò **của schema
        # này** (`<schema>_app`, quyết định D3), không cho `ket_app`. Thiếu bước
        # này thì bảng "không tồn tại" theo cách nhìn của phiên — một thông điệp
        # nghe như migration hỏng trong khi thực ra là chưa nhận đúng vai trò.
        connection.exec_driver_sql(f"SET ROLE {role_name_for_schema(schema)}")
        connection.exec_driver_sql(f'SET search_path TO "{schema}", pg_temp')
        connection.execute(
            text(
                "INSERT INTO warehouses (uid, code, name, path, level, is_group, is_active, "
                "row_version) VALUES (gen_random_uuid(), 'KHO_SAU_HA', 'Kho sau hạ cấp', "
                "'1.', 1, false, true, 1)"
            )
        )
        written = connection.scalars(
            text("SELECT code FROM warehouses WHERE code = 'KHO_SAU_HA'")
        ).all()
    assert list(written) == ["KHO_SAU_HA"]


# ---------------------------------------------------------------------------
# `0010` — fail-closed khi dataset đã có năm tài chính TT99 (sửa sau review, H1)
# ---------------------------------------------------------------------------


def test_downgrading_0010_without_tt99_fiscal_years_succeeds(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """Dataset mới cấp (chưa có năm tài chính nào) — hạ `0010`→`0009` phải chạy
    trót lọt, dọn `config_packages`/`chart_of_accounts` scheme=TT99 (dữ liệu
    gieo tự động) mà không đụng `fiscal_years` (rỗng, không có gì để chặn).
    """
    schema = probe_dataset.schema_name
    _downgrade(owner_engine, schema, "0009")
    assert current_revision(owner_engine, schema) == "0009"

    with owner_engine.begin() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{schema}", pg_temp')
        remaining_tt99 = connection.execute(
            text("SELECT count(*) FROM config_packages WHERE scheme = 'TT99'")
        ).scalar_one()
    assert remaining_tt99 == 0, "downgrade() phải dọn sạch config_packages scheme=TT99"


def test_downgrading_0010_with_a_tt99_fiscal_year_is_refused(
    owner_engine: Engine, session_factory: sessionmaker[Session], probe_dataset: DatasetRef
) -> None:
    """Một năm tài chính TT99 đã tồn tại (quyết định người dùng, không phải dữ
    liệu dựng sẵn) — hạ `0010`→`0009` phải bị từ chối, không âm thầm xóa năm đó
    và cũng không để `CheckViolation` thô của PostgreSQL đổ ra giữa chừng.
    """
    schema = probe_dataset.schema_name
    scope = RequestScope(dataset_schema=schema, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        session.add(Currency(code="VND", name="Đồng Việt Nam", decimal_places=0, is_base=True))
        session.flush()
        PeriodService(session).create_fiscal_year(
            code="2026",
            start_date=date(2026, 1, 1),
            accounting_scheme=AccountingScheme.TT99,
            base_currency="VND",
            inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
            vat_method=VatMethod.DEDUCTION,
        )

    revision_before = current_revision(owner_engine, schema)

    with pytest.raises(RuntimeError, match="TT99"):
        _downgrade(owner_engine, schema, "0009")

    # Hạ cấp bị từ chối phải là TOÀN BỘ-hoặc-KHÔNG-GÌ: revision không nhúc
    # nhích (kể cả các bước TRÊN 0010 đã chạy trót lọt trước khi 0010 từ chối —
    # DDL transactional cuộn lại trọn gói), và năm tài chính vừa tạo vẫn còn
    # nguyên (không bị dọn nhầm). So với revision TRƯỚC lượt hạ chứ không ghim
    # "0010": mỗi migration mới xếp lên đầu sẽ đổi con số đó mà bất biến
    # all-or-nothing không đổi.
    assert current_revision(owner_engine, schema) == revision_before
    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(FiscalYear.code).where(FiscalYear.code == "2026")) == "2026"
