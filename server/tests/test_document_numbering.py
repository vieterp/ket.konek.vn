"""Cấp số chứng từ: định dạng, phạm vi, sổ cấp số, và hoàn số khi rollback.

Phần đồng thời (20 luồng cùng cấp số) nằm riêng ở
`test_concurrent_numbering.py` — nó cần nhiều kết nối thật và chạy chậm hơn hẳn.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import NumberSequenceNotFoundError
from ket.kernel.numbering.models import AllocatedNumber, ResetRule
from ket.kernel.numbering.service import NumberingRule, NumberingService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

INVOICE = NumberingRule(
    document_type="sales.invoice",
    prefix="HD",
    padding=5,
    allow_gaps=False,
    reset_rule=ResetRule.YEARLY,
)
"""Hóa đơn: dãy **liên tục**, có sổ cấp số (BR-EIV-02)."""

CASH_RECEIPT = NumberingRule(
    document_type="cash_book.receipt",
    prefix="PT",
    padding=4,
    allow_gaps=True,
    reset_rule=ResetRule.MONTHLY,
)
"""Phiếu thu: số nội bộ, đứt quãng chấp nhận được."""


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def test_the_scope_key_carries_the_reset_cycle_and_the_branch() -> None:
    """Chu kỳ reset nằm trong khóa, không trong một nhánh `if` lúc cấp số."""
    assert INVOICE.scope_key(branch_id=3, on_date=date(2026, 8, 17)) == (
        "sales.invoice|BRANCH:3|2026"
    )
    assert CASH_RECEIPT.scope_key(branch_id=3, on_date=date(2026, 8, 17)) == (
        "cash_book.receipt|BRANCH:3|2026-08"
    )
    never = NumberingRule(document_type="x", reset_rule=ResetRule.NEVER, per_branch=False)
    assert never.scope_key(branch_id=3, on_date=date(2026, 8, 17)) == "x"


def test_numbers_are_padded_and_prefixed(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = NumberingService(session)
        first = service.allocate(
            INVOICE, branch_id=1, on_date=date(2040, 1, 5), document_id=uuid4()
        )
        second = service.allocate(
            INVOICE, branch_id=1, on_date=date(2040, 1, 6), document_id=uuid4()
        )

        assert first == "HD00001"
        assert second == "HD00002"


def test_a_new_cycle_starts_over_at_one(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Sang tháng mới là sang một dòng bộ đếm mới — không ai phải chạy lệnh reset."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = NumberingService(session)
        service.allocate(CASH_RECEIPT, branch_id=1, on_date=date(2041, 1, 31), document_id=uuid4())
        next_month = service.allocate(
            CASH_RECEIPT, branch_id=1, on_date=date(2041, 2, 1), document_id=uuid4()
        )

        assert next_month == "PT0001"


def test_each_branch_has_its_own_run_of_numbers(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = NumberingService(session)
        service.allocate(INVOICE, branch_id=11, on_date=date(2042, 3, 1), document_id=uuid4())
        other_branch = service.allocate(
            INVOICE, branch_id=12, on_date=date(2042, 3, 1), document_id=uuid4()
        )

        assert other_branch == "HD00001"


def test_a_gap_free_sequence_writes_the_allocation_ledger(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """RT-10: với hóa đơn phải trả lời được "số này đã cấp cho chứng từ nào"."""
    document_id = uuid4()
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        number = NumberingService(session).allocate(
            INVOICE, branch_id=21, on_date=date(2043, 1, 1), document_id=document_id
        )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        row = session.execute(
            select(AllocatedNumber).where(AllocatedNumber.document_id == document_id)
        ).scalar_one()
        assert row.number == number


def test_an_internal_sequence_does_not_write_the_ledger(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Phiếu thu là đường nóng nhất của hệ; một dòng sổ mỗi lần cấp là chi phí thuần."""
    document_id = uuid4()
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        NumberingService(session).allocate(
            CASH_RECEIPT, branch_id=21, on_date=date(2043, 1, 1), document_id=document_id
        )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        rows = session.execute(
            select(AllocatedNumber).where(AllocatedNumber.document_id == document_id)
        ).scalars()
        assert list(rows) == []


def test_rolling_back_the_document_gives_the_number_back(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tiêu chí phase 3: dãy liên tục không được thủng vì một chứng từ ghi hỏng.

    Đây là lý do dịch vụ **không** tự mở transaction — một transaction riêng đã
    commit sẽ tiêu mất số của một chứng từ chưa từng tồn tại.
    """
    rule = NumberingRule(
        document_type="sales.invoice.rollback",
        prefix="HDR",
        allow_gaps=False,
        reset_rule=ResetRule.NEVER,
    )
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert (
            NumberingService(session).allocate(
                rule, branch_id=31, on_date=date(2044, 1, 1), document_id=uuid4()
            )
            == "HDR00001"
        )

    with pytest.raises(RuntimeError, match="chứng từ ghi hỏng"):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            NumberingService(session).allocate(
                rule, branch_id=31, on_date=date(2044, 1, 2), document_id=uuid4()
            )
            raise RuntimeError("chứng từ ghi hỏng")

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert (
            NumberingService(session).allocate(
                rule, branch_id=31, on_date=date(2044, 1, 3), document_id=uuid4()
            )
            == "HDR00002"
        )


def test_peeking_never_consumes_a_number(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = NumberingService(session)
        scope_key = INVOICE.scope_key(branch_id=41, on_date=date(2045, 1, 1))
        service.define(INVOICE, branch_id=41, on_date=date(2045, 1, 1))

        assert service.peek(scope_key) == 1
        assert service.peek(scope_key) == 1
        assert (
            service.allocate(INVOICE, branch_id=41, on_date=date(2045, 1, 1), document_id=uuid4())
            == "HD00001"
        )
        assert service.peek(scope_key) == 2


def test_allocating_from_an_undeclared_sequence_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Không sinh dãy ngầm với giá trị đoán — tiền tố là thứ đã in trên giấy."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(NumberSequenceNotFoundError):
            NumberingService(session).allocate_by_scope_key(
                "khong.ton.tai|BRANCH:1|2046", document_id=uuid4()
            )


def test_the_ledger_refuses_a_duplicate_number_for_the_same_scope(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Hàng rào cuối: nếu một đường ghi ở phase sau quên đi qua dịch vụ.

    Thà một lệnh ghi thất bại còn hơn hai hóa đơn cùng số.
    """
    from sqlalchemy.exc import IntegrityError

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        number = NumberingService(session).allocate(
            INVOICE, branch_id=51, on_date=date(2047, 1, 1), document_id=uuid4()
        )
        scope_key = INVOICE.scope_key(branch_id=51, on_date=date(2047, 1, 1))

    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            session.add(AllocatedNumber(scope_key=scope_key, number=number, document_id=uuid4()))
            session.flush()
