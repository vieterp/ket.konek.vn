"""Hàng đợi + sổ quỹ thủ quỹ trên PostgreSQL thật (SRS 17 §3.1, lát 6C).

Bốn bất biến chính:

* Phân hệ **tắt** (mặc định, FR-WHK-021): phiếu ghi sổ kế toán xong vào thẳng
  sổ quỹ (trạng thái "không áp dụng"), bỏ ghi sổ thì dòng sổ quỹ được gỡ theo.
* Phân hệ **bật**: phiếu chờ trong hàng đợi (BR-WHK-01 — chưa vào sổ quỹ dù đã
  vào sổ kế toán), Ghi sổ quỹ hàng loạt lật trạng thái + ghi dòng, mỗi phiếu
  đúng một lần.
* BR-WHK-05: ngày ghi sổ tùy chọn < ngày hạch toán bị từ chối.
* Hành động Ghi sổ quỹ khi phân hệ tắt / phiếu chưa ghi sổ kế toán bị từ chối
  có thông điệp.

Tùy chọn `treasurer.enabled` là cấp hệ thống trên dataset dùng chung — mỗi
test tự đặt giá trị nó cần ở đầu và trả về "false" trong `finally` để các tệp
test khác không thấy trạng thái lạ.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.config.catalog import TREASURER_ENABLED_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    TreasurerBookDateInvalidError,
    TreasurerModuleDisabledError,
    TreasurerVoucherStateError,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Setting
from ket.modules.cash_book.models import CashVoucher, CashVoucherKind, TreasurerStatus
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.warehousing.treasurer.models import TreasurerCashBookEntry
from ket.modules.warehousing.treasurer.queue_service import book_vouchers, pending_queue
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
TREASURER_ID = 5
JAN_10 = date(2026, 1, 10)


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


def _set_treasurer_enabled(session: Session, enabled: bool) -> None:
    value = "true" if enabled else "false"
    row = session.scalar(
        select(Setting).where(Setting.key == TREASURER_ENABLED_KEY, Setting.scope == "system")
    )
    if row is None:
        session.add(
            Setting(scope="system", key=TREASURER_ENABLED_KEY, value=value, value_type="boolean")
        )
    else:
        row.value = value
    session.flush()


def _receipt(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    amount: int = 400_000,
    posting_date: date = JAN_10,
) -> CashVoucherIn:
    return CashVoucherIn(
        kind=CashVoucherKind.RECEIPT,
        operation_code="thu-khac",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="thu tiền test thủ quỹ",
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["3381"],
                amount_fc=Decimal(amount),
            ),
        ),
    )


def _payment(
    context: PostingContext, accounts: dict[str, int], *, amount: int = 150_000
) -> CashVoucherIn:
    return CashVoucherIn(
        kind=CashVoucherKind.PAYMENT,
        operation_code="chi-khac",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_10,
        posting_date=JAN_10,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="chi tiền test thủ quỹ",
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["1381"],
                credit_account_id=accounts["111"],
                amount_fc=Decimal(amount),
            ),
        ),
    )


def _book_entry_of(session: Session, voucher_id: object) -> TreasurerCashBookEntry | None:
    return session.execute(
        select(TreasurerCashBookEntry).where(TreasurerCashBookEntry.voucher_id == voucher_id)
    ).scalar_one_or_none()


def test_disabled_module_books_straight_to_cash_book_and_unpost_erases(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, False)
            service = CashVoucherService(session)
            voucher = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            service.post(voucher.id, user_id=ACTOR_ID)

            body = session.get(CashVoucher, voucher.id)
            assert body is not None
            assert body.treasurer_status == TreasurerStatus.NOT_APPLICABLE
            entry = _book_entry_of(session, voucher.id)
            assert entry is not None
            assert entry.receipt_amount == Decimal(400_000)
            assert entry.payment_amount == 0
            assert entry.book_date == JAN_10
            assert entry.cash_account_id == accounts["111"]

            service.unpost(voucher.id, user_id=ACTOR_ID)
            assert _book_entry_of(session, voucher.id) is None
            body = session.get(CashVoucher, voucher.id)
            assert body is not None
            assert body.treasurer_status == TreasurerStatus.PENDING
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)


def test_enabled_module_queues_then_books_in_batch(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            receipt = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            payment = service.create(_payment(context, accounts), user_id=ACTOR_ID)
            service.post(receipt.id, user_id=ACTOR_ID)
            service.post(payment.id, user_id=ACTOR_ID)

            # BR-WHK-01: đã vào sổ kế toán nhưng CHƯA vào sổ quỹ.
            assert _book_entry_of(session, receipt.id) is None
            queue_ids = {row.voucher_id for row in pending_queue(session)}
            assert {receipt.id, payment.id} <= queue_ids

            entries = book_vouchers(
                session,
                voucher_ids=(receipt.id, payment.id),
                book_date=None,
                user_id=TREASURER_ID,
            )
            assert len(entries) == 2
            receipt_entry = _book_entry_of(session, receipt.id)
            assert receipt_entry is not None
            assert receipt_entry.receipt_amount == Decimal(400_000)
            assert receipt_entry.book_date == JAN_10
            assert receipt_entry.posted_by == TREASURER_ID
            payment_entry = _book_entry_of(session, payment.id)
            assert payment_entry is not None
            assert payment_entry.payment_amount == Decimal(150_000)

            body = session.get(CashVoucher, receipt.id)
            assert body is not None
            assert body.treasurer_status == TreasurerStatus.BOOKED
            assert body.treasurer_book_date == JAN_10
            assert body.treasurer_posted_by == TREASURER_ID

            # Ghi lần hai phải bị từ chối có thông điệp, không phải IntegrityError.
            with pytest.raises(TreasurerVoucherStateError):
                book_vouchers(
                    session,
                    voucher_ids=(receipt.id,),
                    book_date=None,
                    user_id=TREASURER_ID,
                )
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)


def test_custom_book_date_must_not_precede_posting_date(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            voucher = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            service.post(voucher.id, user_id=ACTOR_ID)

            with pytest.raises(TreasurerBookDateInvalidError):
                book_vouchers(
                    session,
                    voucher_ids=(voucher.id,),
                    book_date=JAN_10 - timedelta(days=1),
                    user_id=TREASURER_ID,
                )
            # Ngày muộn hơn thì hợp lệ (FR-WHK-003).
            book_vouchers(
                session,
                voucher_ids=(voucher.id,),
                book_date=JAN_10 + timedelta(days=2),
                user_id=TREASURER_ID,
            )
            entry = _book_entry_of(session, voucher.id)
            assert entry is not None
            assert entry.book_date == JAN_10 + timedelta(days=2)
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)


def test_booking_requires_enabled_module_and_posted_voucher(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, False)
            service = CashVoucherService(session)
            draft = service.create(_receipt(context, accounts), user_id=ACTOR_ID)

            with pytest.raises(TreasurerModuleDisabledError):
                book_vouchers(
                    session, voucher_ids=(draft.id,), book_date=None, user_id=TREASURER_ID
                )

            _set_treasurer_enabled(session, True)
            # Phiếu Đã cất (chưa ghi sổ kế toán) không vào sổ quỹ được (BR-WHK-01).
            with pytest.raises(TreasurerVoucherStateError):
                book_vouchers(
                    session, voucher_ids=(draft.id,), book_date=None, user_id=TREASURER_ID
                )
            # Danh sách trùng phiếu bị chặn trước khi chạm nguồn.
            with pytest.raises(TreasurerVoucherStateError):
                book_vouchers(
                    session,
                    voucher_ids=(draft.id, draft.id),
                    book_date=None,
                    user_id=TREASURER_ID,
                )
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)
