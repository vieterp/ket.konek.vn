"""Đường NÂNG CẤP bật chiều `bank_account` trên 112x — lát 6G-1 (review H-1).

Cùng điểm mù lịch sử với `test_builtin_print_template_upgrade`: dữ liệu builtin
chỉ được gieo lúc **cấp dataset mới**, nên mọi test chạy trên dataset vừa
provision đều thấy đúng dù migration có làm gì hay không. Dữ liệu kế toán ĐANG
CHẠY thì ngược lại.

Lát 6G-1 suýt ship với đúng cái bẫy ấy: bản đầu bump version gói (3→4, 4→5) và
tin rằng dataset cũ sẽ "nhận backfill". `_seed_one` gặp version lệch thì `return`
TRƯỚC mọi backfill, và trên cả đường gieo mầm không có câu `UPDATE
chart_of_accounts` nào — nó chỉ CHÈN tài khoản cho gói mới. Bump vì thế vừa không
đem chiều tới nơi, vừa tắt luôn hai backfill khác cho mọi dataset cũ. Nay chiều
được bật bằng một câu `UPDATE` tường minh trong migration 0022, và bài kiểm này
là cổng cho ngày ai đó thêm chiều thứ mười hai mà quên viết câu tương ứng.

Vì sao nó QUAN TRỌNG hơn một backfill dữ liệu thường: đường ĐỌC của lát 6G-1 đã
bỏ hết luật suy chủ sở hữu từ `bank_vouchers`. Dataset cũ không bật được chiều
thì validator không đòi ai điền, `gl_postings.bank_account_id` ở lại NULL, và
không còn cơ chế nào bù — sổ chi tiết tiền gửi thiếu vĩnh viễn phần phiếu quỹ và
bút toán tổng hợp chạm 112.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, text

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.datasets.provisioning import (
    ALEMBIC_SCHEMA_ATTRIBUTE,
    DatasetRef,
    drop_dataset_schema,
    find_alembic_config,
    provision_dataset,
    upgrade_dataset_schema,
)

pytestmark = pytest.mark.db

DATASET_CODE = "bank_tracking_probe"

BUILTIN_PACKAGE_CODES = ("TT99-2025", "TT133-2016")
"""Hai gói dựng sẵn — cùng danh sách mà migration 0022 nhắm tới."""


@pytest.fixture
def probe_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    dataset = provision_dataset(
        owner_engine, code=DATASET_CODE, name="Dữ liệu thử chiều TK ngân hàng", scheme="TT99"
    )
    try:
        yield dataset
    finally:
        drop_dataset_schema(owner_engine, DATASET_CODE)


def _downgrade(engine: Engine, schema: str, revision: str) -> None:
    """Hạ một schema dataset xuống một revision — cùng khuôn
    `test_builtin_print_template_upgrade._downgrade`."""
    config = find_alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        config.attributes[ALEMBIC_SCHEMA_ATTRIBUTE] = schema
        try:
            command.downgrade(config, revision)
        finally:
            config.attributes.pop("connection", None)
            config.attributes.pop(ALEMBIC_SCHEMA_ATTRIBUTE, None)


def _tracked_deposit_accounts(engine: Engine, schema: str) -> set[tuple[str, str]]:
    """Cặp (mã gói, số hiệu TK) của mọi TK 112x KHÔNG tổng hợp đã bật chiều."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f'SELECT p.code, a.code FROM "{schema}".chart_of_accounts a '
                f'JOIN "{schema}".config_packages p ON p.id = a.package_id '
                "WHERE a.code LIKE '112%' AND a.is_summary = false "
                "  AND a.detail_tracking @> ARRAY[:tracking]::varchar[]"
            ),
            {"tracking": DetailTracking.BANK_ACCOUNT},
        ).all()
    return {(package, account) for package, account in rows}


def _deposit_accounts(engine: Engine, schema: str) -> set[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f'SELECT p.code, a.code FROM "{schema}".chart_of_accounts a '
                f'JOIN "{schema}".config_packages p ON p.id = a.package_id '
                "WHERE a.code LIKE '112%' AND a.is_summary = false AND p.code = ANY(:packages)"
            ),
            {"packages": list(BUILTIN_PACKAGE_CODES)},
        ).all()
    return {(package, account) for package, account in rows}


def test_a_fresh_dataset_declares_the_dimension_on_every_deposit_account(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """Đường CẤP MỚI đọc thẳng `accounts.csv` — cổng cho ngày ai đó sửa CSV."""
    schema = probe_dataset.schema_name
    expected = _deposit_accounts(owner_engine, schema)
    assert expected, "gói dựng sẵn phải có ít nhất một TK 112 không tổng hợp"
    assert expected <= _tracked_deposit_accounts(owner_engine, schema)


def test_upgrading_an_existing_dataset_turns_the_dimension_on(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """Hạ về `0021`, xóa chiều như một dữ liệu chưa từng thấy nó, nâng lên head
    → chiều quay lại trên ĐÚNG những TK ấy."""
    schema = probe_dataset.schema_name
    expected = _deposit_accounts(owner_engine, schema)
    _downgrade(owner_engine, schema, "0021")
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                f'UPDATE "{schema}".chart_of_accounts SET detail_tracking = NULL '
                "WHERE code LIKE '112%'"
            )
        )
    assert not _tracked_deposit_accounts(owner_engine, schema)

    upgrade_dataset_schema(owner_engine, schema)

    assert expected <= _tracked_deposit_accounts(owner_engine, schema)


def test_turning_the_dimension_on_twice_does_not_duplicate_it(
    owner_engine: Engine, probe_dataset: DatasetRef
) -> None:
    """Câu `UPDATE` phải idempotent: hạ rồi nâng lại trên dataset ĐÃ có chiều
    không được đẩy `bank_account` vào mảng lần thứ hai — một mảng trùng phần tử
    làm validator báo cùng một vi phạm hai lần trên cùng một dòng."""
    schema = probe_dataset.schema_name
    _downgrade(owner_engine, schema, "0021")
    upgrade_dataset_schema(owner_engine, schema)

    with owner_engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT a.code, ("
                "  SELECT count(*) FROM unnest(a.detail_tracking) AS t(v) WHERE t.v = :tracking"
                f') FROM "{schema}".chart_of_accounts a '
                f'JOIN "{schema}".config_packages p ON p.id = a.package_id '
                "WHERE a.code LIKE '112%' AND a.is_summary = false AND p.code = ANY(:packages)"
            ),
            {"tracking": DetailTracking.BANK_ACCOUNT, "packages": list(BUILTIN_PACKAGE_CODES)},
        ).all()
    assert counts, "không có TK 112 nào để kiểm"
    assert all(count == 1 for _, count in counts), counts
