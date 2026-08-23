"""Đường NÂNG CẤP gieo lại mẫu in builtin — lát 6E-2 (review M-4, M21).

Điểm mù lịch sử của repo: dữ liệu builtin chỉ được gieo lúc **cấp dataset mới**,
nên mọi test chạy trên dataset vừa provision đều thấy đủ mẫu bất kể migration
có gieo hay không. Dữ liệu kế toán ĐANG CHẠY thì ngược lại — nó chỉ nhận mẫu
mới qua bước `_refresh_builtin_data` của migration cuối chuỗi (doctrine 5B M-1,
đã cắn ở 6A với gói TT133).

Hai bất biến ghim ở đây:

1. hạ về `0020`, xóa mẫu như một dữ liệu chưa từng thấy chúng, nâng lên head →
   **mẫu quay lại đủ**. Đây là cổng cho ngày ai đó thêm mẫu builtin mà quên dời
   bước làm mới về migration mới nhất;
2. mẫu builtin **không giành lại cờ mặc định** của mẫu người dùng tự đặt —
   FR-RPT-008 cho người dùng sửa/thêm mẫu, và một bản phát hành sau không được
   lặng lẽ đổi tờ giấy mà đơn vị đã chọn in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.printing.models import PrintTemplate
from ket.kernel.datasets.provisioning import (
    ALEMBIC_SCHEMA_ATTRIBUTE,
    DatasetRef,
    drop_dataset_schema,
    find_alembic_config,
    provision_dataset,
    upgrade_dataset_schema,
)
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

DATASET_CODE = "print_seed_probe"

NEW_TEMPLATE_CODES = (
    "PHIEU-THU-01TT",
    "PHIEU-CHI-02TT",
    "UY-NHIEM-CHI",
    "GIAY-BAO-CO",
    "SEC-CHUYEN-KHOAN",
    "CHUYEN-TIEN-NOI-BO",
    "BIEN-BAN-KIEM-KE-QUY-08aTT",
)
"""Mẫu lát 6E-2 thêm — chúng là thứ một dữ liệu đang ở `0020` chưa có."""


@pytest.fixture
def probe_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    dataset = provision_dataset(
        owner_engine, code=DATASET_CODE, name="Dữ liệu thử gieo mẫu in", scheme="TT99"
    )
    try:
        yield dataset
    finally:
        drop_dataset_schema(owner_engine, DATASET_CODE)


def _downgrade(engine: Engine, schema: str, revision: str) -> None:
    """Hạ một schema dataset xuống một revision — cùng khuôn
    `test_dataset_migration_downgrade._downgrade`."""
    config = find_alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        config.attributes[ALEMBIC_SCHEMA_ATTRIBUTE] = schema
        try:
            command.downgrade(config, revision)
        finally:
            config.attributes.pop("connection", None)
            config.attributes.pop(ALEMBIC_SCHEMA_ATTRIBUTE, None)


def _template_codes(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as connection:
        return set(connection.scalars(text(f'SELECT code FROM "{schema}".print_templates')).all())


def test_upgrading_an_existing_dataset_seeds_the_new_builtin_templates(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    schema = probe_dataset.schema_name
    _downgrade(owner_engine, schema, "0020")
    with owner_engine.begin() as connection:
        connection.execute(
            text(f'DELETE FROM "{schema}".print_templates WHERE code = ANY(:codes)'),
            {"codes": list(NEW_TEMPLATE_CODES)},
        )
    assert not set(NEW_TEMPLATE_CODES) & _template_codes(owner_engine, schema)

    upgrade_dataset_schema(owner_engine, schema)

    assert set(NEW_TEMPLATE_CODES) <= _template_codes(owner_engine, schema)


def test_seeding_never_takes_the_default_flag_back_from_a_user_template(
    owner_engine: Engine,
    session_factory: sessionmaker[Session],
    probe_dataset: DatasetRef,
) -> None:
    """Đơn vị đã chọn mẫu riêng làm mặc định thì bản phát hành sau không giành
    lại — index một phần canh bất biến, và seed phải tôn trọng nó."""
    schema = probe_dataset.schema_name
    _downgrade(owner_engine, schema, "0020")
    with owner_engine.begin() as connection:
        connection.execute(
            text(f'DELETE FROM "{schema}".print_templates WHERE code = ANY(:codes)'),
            {"codes": list(NEW_TEMPLATE_CODES)},
        )
        connection.execute(
            text(
                f'INSERT INTO "{schema}".print_templates '
                "(document_type, code, name, html_template, is_default, is_builtin) "
                "VALUES ('PT', 'MAU-RIENG-PT', 'Phiếu thu của đơn vị', '<p>{{ voucher_no }}</p>',"
                " true, false)"
            )
        )

    upgrade_dataset_schema(owner_engine, schema)

    scope = RequestScope(dataset_schema=schema, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        defaults = {
            (row.code, row.is_default)
            for row in session.query(PrintTemplate).filter(PrintTemplate.document_type == "PT")
        }
    assert defaults == {("MAU-RIENG-PT", True), ("PHIEU-THU-01TT", False)}
