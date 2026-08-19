"""`kernel/config/accounts_provider.default_account`/`closing_pairs` (FR-SYS-023/024).

Dùng thẳng gói TT99 dựng sẵn (đã gieo lúc `provision_dataset` — xem
`test_config_package_seed.py`) thay vì dựng gói riêng: đúng dữ liệu mà chứng từ
thật sẽ tra, và không tốn thêm một lượt gieo mầm nữa trong test.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import ClosingAccountPair, ConfigPackage, DefaultAccount
from ket.kernel.config.accounts_provider import closing_pairs, default_account
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import DefaultAccountNotConfiguredError
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def _tt99_package_id(session: Session) -> int:
    package_id = session.scalar(select(ConfigPackage.id).where(ConfigPackage.code == "TT99-2025"))
    assert package_id is not None
    return package_id


def test_default_account_falls_back_to_wildcard_document_type(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _tt99_package_id(session)
        row = default_account(
            session, package_id=package_id, document_type="phieu_thu", purpose="cash"
        )
        assert row.account_code == "111"
        assert row.document_type == "*"


def test_default_account_prefers_specific_document_type_over_wildcard(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Gói TT99 chỉ khai `'*'` cho mọi mục đích ở lát này — dòng riêng theo
    `document_type` cụ thể là cơ chế của resolver, không phải dữ liệu hiện có.
    Kiểm bằng cách tự thêm một dòng riêng rồi xác nhận nó thắng dòng `'*'`.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _tt99_package_id(session)
        session.add(
            DefaultAccount(
                package_id=package_id,
                document_type="phieu_thu",
                purpose="cash",
                account_code="112",
            )
        )
        session.flush()
        row = default_account(
            session, package_id=package_id, document_type="phieu_thu", purpose="cash"
        )
        assert row.account_code == "112"
        session.delete(row)


def test_default_account_missing_raises_typed_error(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _tt99_package_id(session)
        with pytest.raises(DefaultAccountNotConfiguredError):
            default_account(
                session,
                package_id=package_id,
                document_type="phieu_thu",
                purpose="muc_dich_khong_ton_tai",
            )


def test_closing_pairs_are_ordered_by_sequence(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package_id = _tt99_package_id(session)
        rows = closing_pairs(session, package_id=package_id)

        assert len(rows) > 1
        sequences = [row.sequence for row in rows]
        assert sequences == sorted(sequences), "closing_pairs phải trả về đúng thứ tự sequence"
        # Cặp cuối của TT99 luôn là kết chuyển 911 -> 421 (lợi nhuận cuối năm).
        assert rows[-1].source_account == "911"
        assert rows[-1].target_account == "421"


def test_closing_pairs_order_is_the_query_not_insertion_order(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chèn xáo trộn thứ tự `sequence` rồi xác nhận truy vấn vẫn trả đúng thứ
    tự — sửa sau review (M12): dữ liệu seed thật vốn đã chèn đúng thứ tự
    `sequence` (thứ tự vật lý == thứ tự logic một cách trùng hợp), nên
    `test_closing_pairs_are_ordered_by_sequence` ở trên xanh dù có bỏ
    `order_by(sequence)` hay không. Test này chèn NGƯỢC thứ tự để phân biệt
    thật.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package = ConfigPackage(
            code="ZZ-PROBE-CLOSING-ORDER",
            name="Gói giả lập kiểm thứ tự kết chuyển",
            scheme="TT99",
            effective_from=date(2031, 1, 1),
            is_builtin=False,
            activated_at=None,
            activated_by=None,
        )
        session.add(package)
        session.flush()

        # Cố ý chọn `source_account`/thứ tự chèn KHÔNG tương quan với
        # `sequence` theo bất kỳ cách nào dễ đoán (không tăng dần, không giảm
        # dần, không theo thứ tự chèn) — nếu chỉ đơn giản "chèn ngược 30,20,10"
        # thì mã TK vô tình xếp bảng chữ cái đúng khớp thứ tự mong đợi, và
        # PostgreSQL có thể tình cờ dùng chỉ mục duy nhất
        # (`package_id, source_account, target_account`) trả đúng thứ tự đó dù
        # không có `order_by` — che mất chính đột biến đang cần bắt.
        for source, target, sequence in (
            ("333", "911", 10),
            ("777", "632", 30),
            ("111", "421", 20),
        ):
            session.add(
                ClosingAccountPair(
                    package_id=package.id,
                    source_account=source,
                    target_account=target,
                    sequence=sequence,
                )
            )
        session.flush()

        rows = closing_pairs(session, package_id=package.id)
        assert [row.sequence for row in rows] == [10, 20, 30]

        for row in rows:
            session.delete(row)
        session.delete(package)
