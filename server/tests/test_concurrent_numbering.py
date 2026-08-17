"""Số chứng từ không trùng khi nhiều người nhập cùng lúc (FR-NFR-006).

Đây là bài kiểm mà bản thân yêu cầu nói ra: "số chứng từ/hóa đơn không trùng
**khi nhiều người thao tác đồng thời**". Một test tuần tự sẽ xanh với cả cách
làm sai kinh điển (`SELECT MAX(number) + 1`), nên nó không kiểm được gì ở đây.

Chạy thật sự song song bằng thread + `Barrier` để 20 transaction chắc chắn
chồng lên nhau; `NullPool` nên mỗi thread có connection riêng — không có
connection riêng thì SQLAlchemy nối tiếp chúng và cuộc đua biến mất.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.numbering.models import AllocatedNumber, ResetRule
from ket.kernel.numbering.service import NumberingRule, NumberingService
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.settings import Settings

pytestmark = pytest.mark.db

THREADS = 20
ISSUE_DATE = date(2050, 6, 1)

GAP_FREE = NumberingRule(
    document_type="sales.invoice.concurrent",
    prefix="HDĐ",
    padding=5,
    allow_gaps=False,
    reset_rule=ResetRule.NEVER,
    per_branch=False,
)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def _run_together(job: Callable[[], object], threads: int = THREADS) -> list[object]:
    """Chạy `job` trên `threads` luồng, tất cả khởi hành cùng lúc."""
    barrier = threading.Barrier(threads)

    def wrapped() -> object:
        barrier.wait()
        return job()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return [future.result() for future in [pool.submit(wrapped) for _ in range(threads)]]


@pytest.fixture
def independent_factory(test_settings: Settings) -> sessionmaker[Session]:
    """Nhà máy session trên engine `NullPool` — mỗi luồng một connection thật."""
    engine: Engine = create_engine(test_settings.database_url, poolclass=NullPool)
    return create_session_factory(engine)


def test_twenty_concurrent_allocations_produce_an_unbroken_run(
    independent_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tiêu chí phase 3: dãy liên tục, không trùng, không thiếu.

    Kiểm cả ba mặt riêng biệt vì chúng hỏng theo ba kiểu khác nhau: trùng số là
    hai hóa đơn cùng số, thiếu số là một khoảng trống phải giải trình với cơ
    quan thuế, và lệch số lượng là có chứng từ không nhận được số nào.
    """
    scope = _scope(dataset_alpha)

    def allocate() -> str:
        with unit_of_work(independent_factory, scope) as session:
            return NumberingService(session).allocate(
                GAP_FREE, branch_id=None, on_date=ISSUE_DATE, document_id=uuid4()
            )

    numbers = [str(value) for value in _run_together(allocate)]

    assert len(set(numbers)) == THREADS, f"có số trùng: {sorted(numbers)}"
    assert sorted(numbers) == [f"HDĐ{index:05d}" for index in range(1, THREADS + 1)], (
        f"dãy bị thủng hoặc lệch: {sorted(numbers)}"
    )

    # Sổ cấp số phải khớp một-một với số đã phát ra. Kiểm trong cùng test chứ
    # không tách ra: nó nói về **kết quả của chính lượt chạy song song này**, và
    # một test riêng đọc lại bảng sẽ phụ thuộc thứ tự chạy của bộ test.
    scope_key = GAP_FREE.scope_key(branch_id=None, on_date=ISSUE_DATE)
    with unit_of_work(independent_factory, scope) as session:
        ledger = (
            session.execute(
                select(AllocatedNumber.number).where(AllocatedNumber.scope_key == scope_key)
            )
            .scalars()
            .all()
        )

    assert sorted(ledger) == sorted(numbers)
