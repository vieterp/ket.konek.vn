"""`kernel/config/packages/seed.py` — gieo gói dựng sẵn lúc cấp dữ liệu kế toán.

`dataset_alpha`/`dataset_beta` (conftest, session-scoped) đã đi qua
`provision_dataset` — tức đã chạy `ensure_builtin_packages` một lần. Test ở
đây xác nhận: (1) việc gieo đó **thật sự** xảy ra (không phải một hàm gọi mà
không làm gì), và (2) gọi lại hàm gieo trên schema đã có dữ liệu là vô hại
(idempotent theo `code` gói, không nhân đôi dòng).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import (
    BalanceNature,
    ChartOfAccount,
    ClosingAccountPair,
    ConfigPackage,
    DefaultAccount,
)
from ket.kernel.config.packages.seed import BUILTIN_PACKAGE_SLUGS, ensure_builtin_packages
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.seeding import bind_seed_schema
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def test_provisioning_already_seeded_both_builtin_packages(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        codes = set(session.scalars(select(ConfigPackage.code)).all())

    assert "TT99-2025" in codes
    assert "TT133-2016" in codes
    assert len(BUILTIN_PACKAGE_SLUGS) == 2


def test_seeded_package_carries_accounts_defaults_and_closing_pairs(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package = session.scalar(select(ConfigPackage).where(ConfigPackage.code == "TT99-2025"))
        assert package is not None
        assert package.is_builtin is True
        assert package.scheme == "TT99"
        # Gói dựng sẵn kích hoạt ngay lúc gieo (sửa sau review, C1) — nếu
        # không, `resolve_package` (chỉ đọc gói `activated_at IS NOT NULL`)
        # sẽ không thấy nó và mọi chứng từ đầu tiên của dataset mới cấp đổ lỗi
        # "chưa có gói cấu hình nào hiệu lực".
        assert package.activated_at is not None

        accounts = session.scalars(
            select(ChartOfAccount).where(ChartOfAccount.package_id == package.id)
        ).all()
        assert len(accounts) > 50, "hệ thống TK TT99 phải có hàng chục tài khoản"
        by_code = {row.code: row for row in accounts}
        assert by_code["111"].path.endswith(f"{by_code['111'].id}.")
        # TK cấp 2 phải có path là path cha nối thêm id của chính nó.
        child = by_code["1281"]
        parent = by_code["128"]
        assert child.path == f"{parent.path}{child.id}."
        assert child.level == parent.level + 1

        defaults = session.scalars(
            select(DefaultAccount).where(DefaultAccount.package_id == package.id)
        ).all()
        assert any(row.purpose == "cash" for row in defaults)

        pairs = session.scalars(
            select(ClosingAccountPair).where(ClosingAccountPair.package_id == package.id)
        ).all()
        assert len(pairs) > 0


_GOLDEN_TT99_BALANCE_NATURES: dict[str, int] = {
    "111": BalanceNature.DEBIT,
    "131": BalanceNature.DUAL,
    "133": BalanceNature.DEBIT,
    "138": BalanceNature.DEBIT,
    "214": BalanceNature.CREDIT,
    "229": BalanceNature.CREDIT,
    "331": BalanceNature.DUAL,
    "333": BalanceNature.DUAL,
    "338": BalanceNature.DUAL,
    "413": BalanceNature.DUAL,
    "421": BalanceNature.DUAL,
    "511": BalanceNature.NONE,
    "632": BalanceNature.NONE,
    "911": BalanceNature.NONE,
}
"""Mẫu chốt cứng — sửa sau review (H2, khóa đột biến M7).

`balance_nature` sai một TK là số dư sai dấu trên BCTC (FR-NFR-001) và trước
bản sửa này **không có test nào canh** — đổi `111` từ dư Nợ (0) sang dư Có (1)
trong `data/tt99/accounts.csv` vẫn để toàn bộ suite xanh. Mẫu 14 TK ở đây trải
đủ bốn tính chất dư (Nợ/Có/lưỡng tính/không dư) và cả nhóm tài sản, nợ phải
trả, vốn CSH, doanh thu/chi phí — không phải toàn bộ 184 TK (đó là việc của
kế toán trưởng đối chiếu thông tư, không phải việc của một unit test), nhưng
đủ để một lần sửa nhầm nature bị bắt ngay."""


def test_seeded_tt99_accounts_have_the_expected_balance_nature(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package = session.scalar(select(ConfigPackage).where(ConfigPackage.code == "TT99-2025"))
        assert package is not None
        accounts = session.scalars(
            select(ChartOfAccount).where(ChartOfAccount.package_id == package.id)
        ).all()
        by_code = {row.code: row for row in accounts}

        for code, expected_nature in _GOLDEN_TT99_BALANCE_NATURES.items():
            assert by_code[code].balance_nature == expected_nature, (
                f"TK {code} ({by_code[code].name}) lệch balance_nature — kỳ vọng "
                f"{expected_nature}, thấy {by_code[code].balance_nature}"
            )

        # Loại 5-9 (doanh thu, chi phí, thu nhập khác, KQKD) kết chuyển hết cuối
        # kỳ — không TK nào thuộc các loại này được có số dư cuối (BalanceNature.NONE).
        wrong_class_5_to_9 = [
            row.code
            for row in accounts
            if row.code[0] in "56789" and row.balance_nature != BalanceNature.NONE
        ]
        assert not wrong_class_5_to_9, (
            f"TK loại 5-9 phải có balance_nature=3 (không dư): {wrong_class_5_to_9}"
        )


def test_seeding_twice_does_not_duplicate_rows(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        before = (
            connection.execute(select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025"))
            .scalars()
            .all()
        )
        added_second_time = ensure_builtin_packages(connection, dataset_alpha.schema_name)

    assert before, "dataset_alpha phải đã được gieo TT99-2025 từ lúc provision"
    assert added_second_time == 0, "gieo lại trên schema đã có gói không được thêm gì"

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        rows = (
            connection.execute(select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025"))
            .scalars()
            .all()
        )
    assert rows == before, "gieo lại không được tạo dòng `config_packages` thứ hai"


def test_seeded_package_carries_statement_layouts(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Gói builtin gieo lúc provision phải mang layout BCTC (lát 5B)."""
    from ket.kernel.config.statements.models import StatementLayout

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        package_id = connection.execute(
            select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025")
        ).scalar_one()
        codes = set(
            connection.execute(
                select(StatementLayout.code).where(StatementLayout.package_id == package_id)
            ).scalars()
        )
    assert codes == {"B01-DN", "B02-DN"}


def test_reseeding_backfills_missing_statement_layouts_only(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Dataset cấp giữa migration 0010 và 0011 có gói nhưng trống layout —
    lượt gieo kế tiếp phải lấp đúng chỗ trống, không đụng gì khác
    (`seed._ensure_statements_backfilled`)."""
    from sqlalchemy import delete

    from ket.kernel.config.statements.models import StatementLayout, StatementRow

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        package_id = connection.execute(
            select(ConfigPackage.id).where(ConfigPackage.code == "TT133-2016")
        ).scalar_one()
        layout_ids = list(
            connection.execute(
                select(StatementLayout.id).where(StatementLayout.package_id == package_id)
            ).scalars()
        )
        assert layout_ids, "TT133 phải đã có layout từ lúc provision"
        connection.execute(delete(StatementRow).where(StatementRow.layout_id.in_(layout_ids)))
        connection.execute(delete(StatementLayout).where(StatementLayout.id.in_(layout_ids)))

    with owner_engine.begin() as connection:
        added = ensure_builtin_packages(connection, dataset_alpha.schema_name)
    assert added == 0, "backfill layout không được tính là 'gói mới'"

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        codes = set(
            connection.execute(
                select(StatementLayout.code).where(StatementLayout.package_id == package_id)
            ).scalars()
        )
    assert codes == {"B01a-DNN", "B02-DNN"}


def test_backfill_is_skipped_when_package_version_mismatches(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Version gói trong DB lệch với data trên đĩa → KHÔNG backfill layout:
    công thức được loader kiểm dải TK theo accounts.csv CÙNG version trên đĩa,
    gắn vào hệ TK version cũ là đi vòng qua lượt kiểm đó (review 5B, M-1)."""
    from sqlalchemy import delete, update

    from ket.kernel.config.statements.models import StatementLayout, StatementRow

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        package_id = connection.execute(
            select(ConfigPackage.id).where(ConfigPackage.code == "TT133-2016")
        ).scalar_one()
        layout_ids = list(
            connection.execute(
                select(StatementLayout.id).where(StatementLayout.package_id == package_id)
            ).scalars()
        )
        connection.execute(delete(StatementRow).where(StatementRow.layout_id.in_(layout_ids)))
        connection.execute(delete(StatementLayout).where(StatementLayout.id.in_(layout_ids)))
        connection.execute(
            update(ConfigPackage).where(ConfigPackage.id == package_id).values(version=999)
        )

    try:
        with owner_engine.begin() as connection:
            ensure_builtin_packages(connection, dataset_alpha.schema_name)
        with owner_engine.begin() as connection:
            bind_seed_schema(connection, dataset_alpha.schema_name)
            count = (
                connection.execute(
                    select(StatementLayout.id).where(StatementLayout.package_id == package_id)
                )
                .scalars()
                .all()
            )
        assert count == [], "version lệch mà vẫn backfill là đi vòng qua lượt kiểm dải TK"
    finally:
        # Trả version về đúng rồi gieo lại layout — dataset_alpha dùng chung.
        with owner_engine.begin() as connection:
            bind_seed_schema(connection, dataset_alpha.schema_name)
            connection.execute(
                update(ConfigPackage).where(ConfigPackage.id == package_id).values(version=1)
            )
        with owner_engine.begin() as connection:
            ensure_builtin_packages(connection, dataset_alpha.schema_name)
