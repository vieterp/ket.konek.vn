"""`CashBalanceGuard` ba mức (FR-QUY-020, FR-SYS-062) trên PostgreSQL thật — lát 6B.

Cơ chế pipeline (chạy hết guard, đóng dấu `warning=1`) đã kiểm ở
`test_posting_guards.py` với guard giả; ở đây kiểm guard THẬT: đọc cấu hình
`warning.cash_balance`, tính số dư quỹ theo BR-QUY-01 (dư đầu năm + phát sinh
tới ngày hạch toán), và cả nợ 6A — đường "Cất đồng thời ghi sổ" của GLE mang
được xác nhận cảnh báo.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.config.catalog import CASH_BALANCE_WARNING_KEY, SAVE_ALSO_POSTS_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Setting
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.contracts import GUARD_WARNING_DETAIL, VoucherStatus
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_10 = date(2026, 1, 10)


class _RollbackError(Exception):
    """Chặn commit — tùy chọn hệ thống trong test không được dính sang test khác."""


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


def _set_system_setting(session: Session, key: str, value: str, value_type: str) -> None:
    """Upsert thẳng dòng `settings` — cùng lối `test_posting_engine_flow`."""
    row = session.scalar(select(Setting).where(Setting.key == key, Setting.scope == "system"))
    if row is None:
        session.add(Setting(scope="system", key=key, value=value, value_type=value_type))
    else:
        row.value = value
    session.flush()


def _overdraft_payment(
    context: PostingContext, accounts: dict[str, int], amount: int = 300_000
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
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["642"],
                credit_account_id=accounts["111"],
                amount_fc=Decimal(amount),
            ),
        ),
    )


def test_default_level_none_posts_an_overdraft_silently(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(_overdraft_payment(context, accounts), user_id=ACTOR_ID)
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_warn_level_demands_acknowledgement_then_posts(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "warn", "string")
        service = CashVoucherService(session)
        voucher = service.create(_overdraft_payment(context, accounts), user_id=ACTOR_ID)

        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        violation = caught.value.violations[0]
        assert violation.code == "cash.balance_negative"
        assert violation.details[GUARD_WARNING_DETAIL] == 1

        posted = service.post(voucher.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_block_level_ignores_acknowledgement(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "block", "string")
        service = CashVoucherService(session)
        voucher = service.create(_overdraft_payment(context, accounts), user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        violation = caught.value.violations[0]
        assert violation.code == "cash.balance_negative"
        assert GUARD_WARNING_DETAIL not in violation.details
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_balance_counts_prior_postings_on_the_same_code(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Thu 500k trước thì chi 300k không âm → không cảnh báo; chi tiếp 300k
    nữa mới âm — số dư tính từ phát sinh đã ghi, không phải từng chứng từ."""

    def work(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "warn", "string")
        service = CashVoucherService(session)
        receipt = service.create(
            CashVoucherIn(
                kind=CashVoucherKind.RECEIPT,
                operation_code="thu-khac",
                cash_account_id=accounts["111"],
                branch_id=context.branch_id,
                document_date=JAN_10,
                posting_date=JAN_10,
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

        first_payment = service.create(_overdraft_payment(context, accounts), user_id=ACTOR_ID)
        service.post(first_payment.id, user_id=ACTOR_ID)  # 500k − 300k → không âm

        second_payment = service.create(_overdraft_payment(context, accounts), user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError) as caught:
            service.post(second_payment.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "cash.balance_negative"
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_gl_save_also_posts_threads_the_acknowledgement(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Nợ 6A: chứng từ GLE chạm 111x qua đường Cất-đồng-thời-ghi-sổ — lượt đầu
    bị cảnh báo, lượt gửi lại với xác nhận đi trọn trong MỘT transaction."""

    def payload() -> JournalVoucherIn:
        return JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=JAN_10,
            posting_date=JAN_10,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="GLE chạm quỹ",
            lines=(
                JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(90_000)),
                JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(90_000)),
            ),
        )

    def work(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "warn", "string")
        _set_system_setting(session, SAVE_ALSO_POSTS_KEY, "true", "boolean")
        service = JournalVoucherService(session)
        with pytest.raises(PostingValidationError) as caught:
            service.create(payload(), user_id=ACTOR_ID)
        assert caught.value.violations[0].details[GUARD_WARNING_DETAIL] == 1
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)

    def acknowledged(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "warn", "string")
        _set_system_setting(session, SAVE_ALSO_POSTS_KEY, "true", "boolean")
        service = JournalVoucherService(session)
        voucher = service.create(payload(), user_id=ACTOR_ID, acknowledged_warnings=True)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(acknowledged)


def test_a_backdated_payment_that_sinks_a_later_day_is_caught(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Sửa M-2 review 6B: thu 500k ngày 6/1, chi 500k ngày 28/1 (quỹ về 0);
    một phiếu chi LÙI NGÀY 12/1 không âm tại 12/1 (còn 200k) nhưng làm quỹ âm
    từ 28/1 — guard soi số dư THẤP NHẤT từ ngày hạch toán trở đi nên phải bắt."""

    def _cash_move(service: CashVoucherService, *, kind: int, day: date, amount: int) -> None:
        receipt = kind == CashVoucherKind.RECEIPT
        voucher = service.create(
            CashVoucherIn(
                kind=kind,
                operation_code="thu-khac" if receipt else "chi-khac",
                cash_account_id=accounts["111"],
                branch_id=context.branch_id,
                document_date=day,
                posting_date=day,
                currency_code="VND",
                exchange_rate=Decimal(1),
                lines=(
                    CashVoucherLineIn(
                        debit_account_id=accounts["111"] if receipt else accounts["642"],
                        credit_account_id=accounts["3381"] if receipt else accounts["111"],
                        amount_fc=Decimal(amount),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

    def work(session: Session) -> object:
        _set_system_setting(session, CASH_BALANCE_WARNING_KEY, "warn", "string")
        service = CashVoucherService(session)
        _cash_move(service, kind=CashVoucherKind.RECEIPT, day=date(2026, 1, 6), amount=500_000)
        _cash_move(service, kind=CashVoucherKind.PAYMENT, day=date(2026, 1, 28), amount=500_000)

        backdated = service.create(
            _overdraft_payment(context, accounts, amount=300_000).model_copy(
                update={"document_date": date(2026, 1, 12), "posting_date": date(2026, 1, 12)}
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(backdated.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "cash.balance_negative"
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)
