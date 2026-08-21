"""Nghiệp vụ định khoản tự động trên DB thật (FR-SYS-025, lát 6A).

Dùng gói builtin đã gieo lúc `provision_dataset` (như
`test_config_package_accounts_provider.py`). Test provider trỏ thẳng
`operations_in_package` vào gói builtin theo `code` — không đi qua
`resolve_package`, vì các test posting khác trong cùng dataset dựng gói
`TT99-TEST` kích hoạt sau và sẽ thắng lượt phân giải (xem
`posting_support._ensure_package_and_accounts`).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.config.auto_posting_provider import operations_in_package
from ket.kernel.config.packages.seed import ensure_builtin_packages
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.seeding import bind_seed_schema
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def _package_id(session: Session, code: str) -> int:
    package_id = session.scalar(select(ConfigPackage.id).where(ConfigPackage.code == code))
    assert package_id is not None, f"gói {code} phải đã được gieo lúc provision"
    return package_id


def test_provisioning_seeded_rules_for_both_builtin_packages(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        for code in ("TT99-2025", "TT133-2016"):
            count = session.scalar(
                select(AutoPostingRule.id)
                .where(AutoPostingRule.package_id == _package_id(session, code))
                .limit(1)
            )
            assert count is not None, f"gói {code} chưa có nghiệp vụ nào"


def test_tt99_receipt_operations_resolve_debit_and_credit_codes(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        resolved = operations_in_package(
            session, package_id=_package_id(session, "TT99-2025"), document_type="PT"
        )
        by_code = {item.operation_code: item for item in resolved.items}

        thu_no = by_code["thu-no-khach-hang"]
        assert thu_no.debit_account_code == "111"
        assert thu_no.credit_account_code == "131"
        assert thu_no.requires_partner is True
        assert thu_no.partner_kind == 0

        # "Thu khác" cố ý để ngỏ bên Có — SRS liệt kê cả chục TK khả dĩ.
        assert by_code["thu-khac"].credit_account_code is None
        assert by_code["thu-khac"].debit_account_code == "111"

        # Danh sách trả theo `display_order` — thứ tự SRS 03 §3.1.
        orders = [item.display_order for item in resolved.items]
        assert orders == sorted(orders)


def test_tt133_resolves_detail_cash_accounts_and_leaves_insurance_open(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """TT133: 111/112 là TK tổng hợp nên purpose cash/bank trỏ 1111/1121;
    gói chưa khai TK bảo hiểm chi tiết nên `nop-bao-hiem` để ngỏ bên Nợ —
    "thiếu purpose" là quyết định của gói, không phải lỗi 500."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _package_id(session, "TT133-2016")
        receipts = operations_in_package(session, package_id=package_id, document_type="PT")
        by_code = {item.operation_code: item for item in receipts.items}
        assert by_code["thu-no-khach-hang"].debit_account_code == "1111"

        payments = operations_in_package(session, package_id=package_id, document_type="UNC")
        insurance = {item.operation_code: item for item in payments.items}["nop-bao-hiem"]
        assert insurance.debit_account_code is None
        assert insurance.credit_account_code == "1121"


def test_an_unknown_document_type_yields_an_empty_list(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        resolved = operations_in_package(
            session, package_id=_package_id(session, "TT99-2025"), document_type="KHONG_CO"
        )
        assert resolved.items == ()


def test_reseeding_backfills_missing_rules_only(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Dataset cấp trước 0015 có gói nhưng trống nghiệp vụ — lượt gieo kế tiếp
    lấp đúng chỗ trống (`seed._ensure_auto_posting_backfilled`), không tính là
    gói mới."""
    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        package_id = connection.execute(
            select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025")
        ).scalar_one()
        before = set(
            connection.execute(
                select(AutoPostingRule.document_type, AutoPostingRule.operation_code).where(
                    AutoPostingRule.package_id == package_id
                )
            ).all()
        )
        assert before, "TT99 phải đã có nghiệp vụ từ lúc provision"
        connection.execute(delete(AutoPostingRule).where(AutoPostingRule.package_id == package_id))

    with owner_engine.begin() as connection:
        added = ensure_builtin_packages(connection, dataset_alpha.schema_name)
    assert added == 0, "backfill nghiệp vụ không được tính là 'gói mới'"

    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        after = set(
            connection.execute(
                select(AutoPostingRule.document_type, AutoPostingRule.operation_code).where(
                    AutoPostingRule.package_id == package_id
                )
            ).all()
        )
    assert after == before


def test_a_specific_document_type_default_beats_the_wildcard(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Resolver phải theo đúng luật `default_account`: dòng `(PT, cash)` thắng
    dòng `('*', cash)` (review 6A, M12 — trước đó không test nào phân biệt,
    đảo thứ tự fallback vẫn xanh)."""
    from ket.kernel.config.accounts_models import DefaultAccount

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _package_id(session, "TT99-2025")
        override = DefaultAccount(
            package_id=package_id, document_type="PT", purpose="cash", account_code="112"
        )
        session.add(override)
        session.flush()
        try:
            receipts = operations_in_package(session, package_id=package_id, document_type="PT")
            by_code = {item.operation_code: item for item in receipts.items}
            assert by_code["thu-khac"].debit_account_code == "112"
            payments = operations_in_package(session, package_id=package_id, document_type="PC")
            assert {i.operation_code: i for i in payments.items}[
                "chi-khac"
            ].credit_account_code == "111", "loại chứng từ khác vẫn theo wildcard"
        finally:
            session.delete(override)


def test_a_version_mismatched_package_receives_no_backfill(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Ghim hệ quả của bump version tt133 (review 6A, H-2, controller chốt
    chấp-nhận-và-ghi-nhận): dataset mang gói version cũ KHÔNG nhận backfill —
    cùng doctrine 5B M-1: nghiệp vụ v2 tham chiếu purpose/TK được loader kiểm
    theo dữ liệu CÙNG version trên đĩa, gắn vào gói version cũ trong DB là đi
    vòng qua chính lượt kiểm đó. Đường sửa cho bản cài thật là nâng cấp gói có
    kiểm soát (phase 11), không phải đường gieo mầm; trước phát hành thì
    dataset dev cấp lại. Nửa sau của test: trả version về khớp → backfill chạy."""
    from sqlalchemy import update

    code = "TT133-2016"
    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        package_id = connection.execute(
            select(ConfigPackage.id).where(ConfigPackage.code == code)
        ).scalar_one()
        disk_version = connection.execute(
            select(ConfigPackage.version).where(ConfigPackage.id == package_id)
        ).scalar_one()
        connection.execute(
            update(ConfigPackage).where(ConfigPackage.id == package_id).values(version=1)
        )
        connection.execute(delete(AutoPostingRule).where(AutoPostingRule.package_id == package_id))

    with owner_engine.begin() as connection:
        assert ensure_builtin_packages(connection, dataset_alpha.schema_name) == 0
    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        leftover = connection.execute(
            select(AutoPostingRule.id).where(AutoPostingRule.package_id == package_id).limit(1)
        ).scalar_one_or_none()
        assert leftover is None, "version lệch mà vẫn backfill là đi vòng qua lượt kiểm loader"
        connection.execute(
            update(ConfigPackage).where(ConfigPackage.id == package_id).values(version=disk_version)
        )

    with owner_engine.begin() as connection:
        assert ensure_builtin_packages(connection, dataset_alpha.schema_name) == 0
    with owner_engine.begin() as connection:
        bind_seed_schema(connection, dataset_alpha.schema_name)
        refilled = connection.execute(
            select(AutoPostingRule.id).where(AutoPostingRule.package_id == package_id).limit(1)
        ).scalar_one_or_none()
        assert refilled is not None, "version khớp trở lại thì backfill phải lấp chỗ trống"
