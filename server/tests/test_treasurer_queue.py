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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.config.catalog import TREASURER_ENABLED_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    TreasurerBookDateInvalidError,
    TreasurerVoucherStateError,
)
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Setting
from ket.modules.cash_book.models import CashVoucher, CashVoucherKind, TreasurerStatus
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.warehousing.treasurer.models import TreasurerCashBookEntry
from ket.modules.warehousing.treasurer.queue_service import book_vouchers, pending_queue
from ket.posting.integrity.checks.registry import check_of
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


def test_booking_rejects_drafts_and_duplicates_but_survives_module_toggle_off(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            draft = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
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
            with pytest.raises(TreasurerVoucherStateError):
                book_vouchers(session, voucher_ids=(), book_date=None, user_id=TREASURER_ID)

            # Review 6C M-1: phiếu treo trạng thái chờ vì phân hệ bị TẮT giữa
            # chừng vẫn ghi sổ quỹ được — nếu không, sổ quỹ thiếu vĩnh viễn.
            leftover = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            service.post(leftover.id, user_id=ACTOR_ID)
            _set_treasurer_enabled(session, False)
            entries = book_vouchers(
                session, voucher_ids=(leftover.id,), book_date=None, user_id=TREASURER_ID
            )
            assert len(entries) == 1
            assert _book_entry_of(session, leftover.id) is not None
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)


def test_net_zero_voucher_never_enters_the_queue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review 6C H-2: phiếu không đồng nào chạm TK quỹ của thân phiếu (định
    khoản tự do) không được nằm trong hàng đợi — nó không bao giờ ghi được và
    đầu độc cả lô all-or-nothing."""

    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            payload = _receipt(context, accounts).model_copy(
                update={
                    "lines": (
                        CashVoucherLineIn(
                            debit_account_id=accounts["1381"],
                            credit_account_id=accounts["3381"],
                            amount_fc=Decimal(50_000),
                        ),
                    )
                }
            )
            voucher = service.create(payload, user_id=ACTOR_ID)
            service.post(voucher.id, user_id=ACTOR_ID)

            body = session.get(CashVoucher, voucher.id)
            assert body is not None
            assert body.treasurer_status == TreasurerStatus.NOT_APPLICABLE
            assert voucher.id not in {row.voucher_id for row in pending_queue(session)}
            assert _book_entry_of(session, voucher.id) is None
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)


def test_out_of_scope_voucher_is_a_state_error_not_a_500(
    run: Runner,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Review 6C H-1: thủ quỹ scope chi nhánh khác book một phiếu ngoài phạm vi
    — header ẩn dưới RLS phải thành lỗi nghiệp vụ CÙNG thông điệp với "không
    phải phiếu quỹ" (chống oracle), không phải RuntimeError 500; và không được
    cầm khóa trên hàng của chi nhánh khác trước khi nổ."""
    voucher_id_holder: list[object] = []

    def prepare(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            voucher = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            service.post(voucher.id, user_id=ACTOR_ID)
            voucher_id_holder.append(voucher.id)
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(prepare)

    foreign_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=TREASURER_ID,
        branch_ids=(context.branch_id + 987_654,),
        acting_branch_id=context.branch_id + 987_654,
    )
    with unit_of_work(session_factory, foreign_scope) as session:
        with pytest.raises(TreasurerVoucherStateError):
            book_vouchers(
                session,
                voucher_ids=(voucher_id_holder[0],),  # type: ignore[arg-type]
                book_date=None,
                user_id=TREASURER_ID,
            )


def test_integrity_check_flags_tampered_treasurer_book(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review 6C M-2: thực thi `treasurer_book_matches_ledger` thật — im lặng
    trên dữ liệu lành, bắt từng trạng thái không-bao-giờ-hợp-lệ, và trả về im
    lặng sau khi khôi phục (dataset dùng chung phải sạch khi test kết thúc)."""

    def rows_for_branch(session: Session) -> list[object]:
        sql = check_of("treasurer_book_matches_ledger").sql()
        return list(session.execute(text(sql), {"branch_id": context.branch_id}).all())

    def work(session: Session) -> object:
        try:
            _set_treasurer_enabled(session, True)
            service = CashVoucherService(session)
            voucher = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
            service.post(voucher.id, user_id=ACTOR_ID)
            book_vouchers(session, voucher_ids=(voucher.id,), book_date=None, user_id=TREASURER_ID)
            assert rows_for_branch(session) == []

            # Tamper 1: dòng sổ quỹ lệch số với sổ kế toán.
            entry = _book_entry_of(session, voucher.id)
            assert entry is not None
            original = entry.receipt_amount
            entry.receipt_amount = original + Decimal(1)
            session.flush()
            flagged = rows_for_branch(session)
            assert any(str(row[0]) == str(voucher.id) for row in flagged)
            entry.receipt_amount = original
            session.flush()

            # Tamper 2: phiếu mang trạng thái BOOKED nhưng dòng sổ quỹ biến mất.
            session.delete(entry)
            session.flush()
            flagged = rows_for_branch(session)
            assert any(str(row[0]) == str(voucher.id) for row in flagged)

            # Khôi phục: trả phiếu về trạng thái chờ (dòng đã xóa) — hàng đợi
            # thấy lại phiếu, check im lặng (BR-WHK-01: chờ là hợp lệ).
            body = session.get(CashVoucher, voucher.id)
            assert body is not None
            body.treasurer_status = TreasurerStatus.PENDING
            body.treasurer_book_date = None
            body.treasurer_posted_at = None
            body.treasurer_posted_by = None
            session.flush()
            assert rows_for_branch(session) == []

            # Tamper 3: dòng sổ quỹ tồn tại khi phiếu còn trạng thái chờ.
            book_vouchers(session, voucher_ids=(voucher.id,), book_date=None, user_id=TREASURER_ID)
            body.treasurer_status = TreasurerStatus.PENDING
            body.treasurer_book_date = None
            body.treasurer_posted_at = None
            body.treasurer_posted_by = None
            session.flush()
            flagged = rows_for_branch(session)
            assert any(str(row[0]) == str(voucher.id) for row in flagged)

            # Khôi phục trạng thái nhất quán cuối cùng: phiếu đã ghi sổ quỹ.
            body.treasurer_status = TreasurerStatus.BOOKED
            body.treasurer_book_date = JAN_10
            body.treasurer_posted_by = TREASURER_ID
            body.treasurer_posted_at = datetime.now(UTC)
            session.flush()
            assert rows_for_branch(session) == []
        finally:
            _set_treasurer_enabled(session, False)
        return None

    run(work)
