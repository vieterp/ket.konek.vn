"""Kiểm kê quỹ (FR-QUY-030/031) trên PostgreSQL thật — lát 6B.

Biên bản chụp số sổ tại ngày kiểm; chênh lệch sinh phiếu qua đúng nghiệp vụ
của gói (`thua-quy-kiem-ke`/`thieu-quy-kiem-ke`) với TK đối ứng lấy từ
`default_accounts` — không số hiệu nào cứng trong code.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import CountSheetAdjustmentError, CountSheetInvalidError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.cash_book.count_sheet_service import CashCountSheetService
from ket.modules.cash_book.models import CashVoucher, CashVoucherKind
from ket.modules.cash_book.schemas import (
    CashVoucherIn,
    CashVoucherLineIn,
    CountSheetIn,
    CountSheetLineIn,
)
from ket.modules.cash_book.service import CashVoucherService
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_31 = date(2026, 1, 31)


class _RollbackError(Exception):
    """Chặn commit — mỗi test tự dọn số dư của mình trên chi nhánh dùng chung."""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    return seed_cash_book_package_data(session_factory, dataset_alpha, context)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _book_500k(session: Session, context: PostingContext, accounts: dict[str, int]) -> None:
    """Ghi 500k vào quỹ để biên bản có số sổ khác 0."""
    service = CashVoucherService(session)
    receipt = service.create(
        CashVoucherIn(
            kind=CashVoucherKind.RECEIPT,
            operation_code="thu-khac",
            cash_account_id=accounts["111"],
            branch_id=context.branch_id,
            document_date=JAN_31,
            posting_date=JAN_31,
            currency_code="VND",
            exchange_rate=Decimal(1),
            lines=(
                CashVoucherLineIn(
                    debit_account_id=accounts["111"],
                    credit_account_id=accounts["3381"],
                    amount_fc=Decimal(500_000),
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(receipt.id, user_id=ACTOR_ID)


def _sheet(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    counted: int,
    lines: tuple[CountSheetLineIn, ...] = (),
) -> CountSheetIn:
    return CountSheetIn(
        branch_id=context.branch_id,
        cash_account_id=accounts["111"],
        count_date=JAN_31,
        counted_total=Decimal(counted),
        lines=lines,
    )


def test_sheet_snapshots_book_balance_and_denominations_must_sum(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _book_500k(session, context, accounts)
        service = CashCountSheetService(session)
        sheet = service.create(
            _sheet(
                context,
                accounts,
                counted=500_000,
                lines=(CountSheetLineIn(denomination=Decimal(500_000), quantity=1),),
            ),
            user_id=ACTOR_ID,
        )
        assert sheet.book_balance == Decimal(500_000)
        assert sheet.counted_total == Decimal(500_000)

        with pytest.raises(CountSheetInvalidError):
            service.create(
                _sheet(
                    context,
                    accounts,
                    counted=400_000,
                    lines=(CountSheetLineIn(denomination=Decimal(100_000), quantity=1),),
                ),
                user_id=ACTOR_ID,
            )

        with pytest.raises(CountSheetAdjustmentError):
            service.create_adjustment(sheet.id, user_id=ACTOR_ID)  # không chênh lệch
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_surplus_creates_a_receipt_against_the_configured_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _book_500k(session, context, accounts)
        service = CashCountSheetService(session)
        sheet = service.create(_sheet(context, accounts, counted=520_000), user_id=ACTOR_ID)
        voucher = service.create_adjustment(sheet.id, user_id=ACTOR_ID)
        assert voucher.document_type == "PT"

        body = session.get(CashVoucher, voucher.id)
        assert body is not None and body.operation_code == "thua-quy-kiem-ke"
        _, _, lines, _ = CashVoucherService(session).get(voucher.id)
        assert lines[0].debit_account_id == accounts["111"]
        assert lines[0].credit_account_id == accounts["3381"]
        assert lines[0].amount_fc == Decimal(20_000)

        refreshed = service.require(sheet.id)
        assert refreshed.adjustment_voucher_id == voucher.id
        with pytest.raises(CountSheetAdjustmentError):
            service.create_adjustment(sheet.id, user_id=ACTOR_ID)  # đã xử lý rồi
        with pytest.raises(CountSheetAdjustmentError):
            service.delete(sheet.id)  # biên bản là căn cứ của phiếu
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_deficit_creates_a_payment_debiting_shortage_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _book_500k(session, context, accounts)
        service = CashCountSheetService(session)
        sheet = service.create(_sheet(context, accounts, counted=450_000), user_id=ACTOR_ID)
        voucher = service.create_adjustment(sheet.id, user_id=ACTOR_ID)
        assert voucher.document_type == "PC"

        _, _, lines, _ = CashVoucherService(session).get(voucher.id)
        assert lines[0].debit_account_id == accounts["1381"]
        assert lines[0].credit_account_id == accounts["111"]
        assert lines[0].amount_fc == Decimal(50_000)
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_only_cash_accounts_can_be_counted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        with pytest.raises(CountSheetInvalidError):
            CashCountSheetService(session).create(
                CountSheetIn(
                    branch_id=context.branch_id,
                    cash_account_id=accounts["131"],
                    count_date=JAN_31,
                    counted_total=Decimal(0),
                ),
                user_id=ACTOR_ID,
            )
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_delete_without_adjustment_removes_the_sheet(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashCountSheetService(session)
        sheet = service.create(_sheet(context, accounts, counted=0), user_id=ACTOR_ID)
        service.delete(sheet.id)
        rows, total = service.list_page(
            cash_account_id=accounts["111"], from_date=None, to_date=None, page=1, page_size=10
        )
        assert all(row.id != sheet.id for row in rows)
        assert total == len(rows)
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_concurrent_create_adjustment_yields_exactly_one_voucher(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Sửa H-3 review 6B: hai request create-adjustment đồng thời trên một biên
    bản chỉ được sinh MỘT phiếu — `FOR UPDATE` trên biên bản nối tiếp hai txn,
    txn sau thấy `adjustment_voucher_id` đã có và bị từ chối."""
    import threading

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        sheet = CashCountSheetService(session).create(
            _sheet(context, accounts, counted=40_000), user_id=ACTOR_ID
        )
        sheet_id = sheet.id

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            with unit_of_work(session_factory, scope) as session:
                CashCountSheetService(session).create_adjustment(sheet_id, user_id=ACTOR_ID)
            outcome = "created"
        except CountSheetAdjustmentError:
            outcome = "refused"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "refused"]
    with unit_of_work(session_factory, scope) as session:
        service = CashCountSheetService(session)
        refreshed = service.require(sheet_id)
        assert refreshed.adjustment_voucher_id is not None
        # Dọn: gỡ phiếu + biên bản để không rơi vãi vào chi nhánh dùng chung.
        from ket.modules.cash_book.service import CashVoucherService

        CashVoucherService(session).delete(refreshed.adjustment_voucher_id)
        # FK `SET NULL` chạy ở DB — làm mới identity map trước khi đọc lại.
        session.expire_all()
        refreshed_again = service.require(sheet_id)
        assert refreshed_again.adjustment_voucher_id is None
        service.delete(sheet_id)
