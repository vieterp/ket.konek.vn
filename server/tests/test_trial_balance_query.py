"""Bảng cân đối tài khoản — hai đường đọc phải cho CÙNG một con số (4B).

Hợp đồng của `query_service.trial_balance` (phase-04 §Snapshot):

* kỳ còn dấu bẩn → tính thẳng từ `gl_postings` + `opening_balances`, `stale=True`;
* kỳ sạch → đọc snapshot `account_balances`, `stale=False`;
* **hai đường cho kết quả giống hệt** — nếu lệch thì một trong hai phép toán
  sai, và "chậm nhưng luôn đúng" thành "nhanh và sai lúc nào không biết".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PeriodNotFoundError
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances import recalc
from ket.posting.balances.query_service import TrialBalanceResult, trial_balance
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.models import OpeningBalance
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"
USD = "USD"
JAN_15 = date(2026, 1, 15)
FEB_10 = date(2026, 2, 10)


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


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


def _post(
    session: Session,
    context: PostingContext,
    *,
    posting_date: date,
    amount: int,
    currency: str = VND,
    rate: Decimal = Decimal(1),
    debit_account: str = "642",
    credit_account: str = "111",
) -> None:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=currency,
            exchange_rate=rate,
            description="chứng từ test bảng cân đối",
            lines=(
                JournalLineIn(account_id=context.accounts[debit_account], debit_fc=Decimal(amount)),
                JournalLineIn(
                    account_id=context.accounts[credit_account], credit_fc=Decimal(amount)
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)


def _recalc_branch(session: Session, branch_id: int) -> None:
    marks = recalc.pending_marks(session, branch_id=branch_id)
    for one_run in recalc.build_runs(session, marks):
        for period in one_run.periods:
            recalc.recalc_period(session, ledger=one_run.ledger, branch_id=branch_id, period=period)
    recalc.clear_marks(session, branch_id=branch_id, marks=marks)


def _period_id(session: Session, context: PostingContext, month: int) -> int:
    return session.execute(
        select(AccountingPeriod.id).where(
            AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
            AccountingPeriod.period_no == month,
        )
    ).scalar_one()


def _comparable(result: TrialBalanceResult) -> list[tuple[object, ...]]:
    return [
        (
            row.account_code,
            row.opening_debit,
            row.opening_credit,
            row.period_debit,
            row.period_credit,
            row.closing_debit,
            row.closing_credit,
        )
        for row in result.rows
    ]


def test_dirty_period_computes_directly_and_matches_snapshot_after_recalc(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        session.add(
            OpeningBalance(
                fiscal_year_id=context.fiscal_year_id,
                ledger=Ledger.FINANCIAL.value,
                branch_id=context.branch_id,
                account_id=context.accounts["111"],
                currency_code=VND,
                debit=Decimal("1000000.00"),
                debit_fc=Decimal("1000000.00"),
                detail_kind=0,
            )
        )
        session.flush()
        _post(session, context, posting_date=JAN_15, amount=100_000)
        _post(session, context, posting_date=FEB_10, amount=40_000)
        # USD tháng 2: 10 USD × 25.000 — kiểm quy đổi đi vào đúng cột.
        _post(
            session,
            context,
            posting_date=FEB_10,
            amount=10,
            currency=USD,
            rate=Decimal(25_000),
        )

        feb = _period_id(session, context, 2)
        dirty = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert dirty.stale is True

        cash = next(row for row in dirty.rows if row.account_code == "111")
        # Dư đầu tháng 2 = số dư ban đầu 1.000.000 − 100.000 của tháng 1.
        assert cash.opening_debit == Decimal("900000.00")
        # Phát sinh tháng 2 = 40.000 VND + 10 USD × 25.000.
        assert cash.period_credit == Decimal("290000.00")
        assert cash.closing_debit == Decimal("610000.00")

        _recalc_branch(session, context.branch_id)
        clean = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert clean.stale is False
        # Bất biến trung tâm: snapshot và tính-thẳng cho cùng một bảng.
        assert _comparable(clean) == _comparable(dirty)
        return None

    run(work)


def test_dirty_mark_in_earlier_period_stains_later_periods_of_same_year(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        _post(session, context, posting_date=FEB_10, amount=50_000)
        _recalc_branch(session, context.branch_id)
        feb = _period_id(session, context, 2)
        assert trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb).stale is False

        # Ghi lùi vào tháng 1 → tháng 2 phải tự coi là bẩn dù dấu ghi ở kỳ 1.
        _post(session, context, posting_date=JAN_15, amount=70_000)
        assert trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb).stale is True

        jan = _period_id(session, context, 1)
        assert trial_balance(session, ledger=Ledger.FINANCIAL, period_id=jan).stale is True
        return None

    run(work)


def test_ledgers_do_not_leak_into_each_other(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        _post(session, context, posting_date=JAN_15, amount=100_000)
        _recalc_branch(session, context.branch_id)
        jan = _period_id(session, context, 1)

        financial = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=jan)
        management = trial_balance(session, ledger=Ledger.MANAGEMENT, period_id=jan)
        # GLE nhân bản hai sổ nên số giống nhau — nhưng phải là hai lượt đọc
        # độc lập, không phải một bảng dùng chung.
        assert _comparable(financial) == _comparable(management)

        session.add(
            OpeningBalance(
                fiscal_year_id=context.fiscal_year_id,
                ledger=Ledger.MANAGEMENT.value,
                branch_id=context.branch_id,
                account_id=context.accounts["112"],
                currency_code=VND,
                debit=Decimal("77000.00"),
                debit_fc=Decimal("77000.00"),
                detail_kind=0,
            )
        )
        session.flush()
        # Nhập số dư ban đầu phải kèm dấu bẩn (hợp đồng cho service 4C) —
        # ở đây đánh tay để kiểm đường đọc.
        mark_dirty(
            session,
            ledger=Ledger.MANAGEMENT.value,
            branch_id=context.branch_id,
            from_period_id=jan,
            reason="nhập số dư đầu kỳ sổ quản trị",
        )
        stained = trial_balance(session, ledger=Ledger.MANAGEMENT, period_id=jan)
        assert stained.stale is True
        assert "112" in {row.account_code for row in stained.rows}

        untouched = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=jan)
        # Dấu bẩn của sổ quản trị không làm bẩn sổ tài chính, và số dư ban đầu
        # của sổ quản trị không lọt sang.
        assert untouched.stale is False
        assert "112" not in {row.account_code for row in untouched.rows}
        return None

    run(work)


def test_branch_filter_narrows_within_rls_scope(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        _post(session, context, posting_date=JAN_15, amount=100_000)
        _recalc_branch(session, context.branch_id)
        jan = _period_id(session, context, 1)

        own = trial_balance(
            session, ledger=Ledger.FINANCIAL, period_id=jan, branch_id=context.branch_id
        )
        assert own.rows

        # Chi nhánh ngoài phạm vi → bảng rỗng, không phải lỗi (RLS đã lọc).
        other = trial_balance(
            session, ledger=Ledger.FINANCIAL, period_id=jan, branch_id=context.branch_id + 1
        )
        assert other.rows == ()
        return None

    run(work)


def test_direct_path_respects_period_day_boundaries(run: Runner, context: PostingContext) -> None:
    """Off-by-one biên ngày của đường tính-thẳng (đột biến M08/M09 review 4B).

    Chứng từ ĐÚNG ngày đầu kỳ phải vào phát sinh (không vào dư đầu), chứng từ
    ngày cuối kỳ trước phải vào dư đầu — mọi test khác post giữa kỳ nên chỉ
    case này canh được dấu `<`/`>=` trong `trial_balance_direct.sql`.
    """

    def work(session: Session) -> object:
        _post(session, context, posting_date=date(2026, 1, 31), amount=70_000)
        _post(session, context, posting_date=date(2026, 2, 1), amount=40_000)
        _post(session, context, posting_date=date(2026, 2, 28), amount=5_000)

        feb = _period_id(session, context, 2)
        dirty = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert dirty.stale is True
        cash = next(row for row in dirty.rows if row.account_code == "111")
        # Dư đầu tháng 2 = đúng chứng từ 31/01; phát sinh = 01/02 + 28/02.
        assert cash.opening_credit == Decimal("70000.00")
        assert cash.period_credit == Decimal("45000.00")

        _recalc_branch(session, context.branch_id)
        clean = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert clean.stale is False
        assert _comparable(clean) == _comparable(dirty)
        return None

    run(work)


def test_direct_path_suppresses_settled_accounts_like_snapshot(
    run: Runner, context: PostingContext
) -> None:
    """TK đã tất toán không được hiện dòng 0 ở đường tính-thẳng (đột biến M10).

    Kỳ có phát sinh thì giữ dòng (kể cả tất toán về 0 trong kỳ); kỳ sau không
    còn gì thì cả hai đường cùng ẨN — kỳ bẩn không được "mọc thêm" dòng 0 so
    với kỳ sạch.
    """

    def work(session: Session) -> object:
        # Tháng 1: 112 nhận 60k rồi trả đúng 60k — tất toán trong kỳ.
        _post(session, context, posting_date=JAN_15, amount=60_000, debit_account="112")
        _post(
            session,
            context,
            posting_date=JAN_15,
            amount=60_000,
            debit_account="111",
            credit_account="112",
        )

        jan = _period_id(session, context, 1)
        feb = _period_id(session, context, 2)

        jan_dirty = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=jan)
        assert jan_dirty.stale is True
        codes_jan = {row.account_code for row in jan_dirty.rows}
        # Kỳ có phát sinh → giữ dòng dù dư cuối 0.
        assert "112" in codes_jan

        feb_dirty = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert feb_dirty.stale is True
        assert "112" not in {row.account_code for row in feb_dirty.rows}

        _recalc_branch(session, context.branch_id)
        feb_clean = trial_balance(session, ledger=Ledger.FINANCIAL, period_id=feb)
        assert _comparable(feb_clean) == _comparable(feb_dirty)
        return None

    run(work)


def test_unknown_period_raises_domain_error(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        with pytest.raises(PeriodNotFoundError):
            trial_balance(session, ledger=Ledger.FINANCIAL, period_id=999_999)
        return None

    run(work)
