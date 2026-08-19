"""Tính lại snapshot số dư trên PostgreSQL thật — nửa "đọc" của ADR-011 (4B).

Bốn nhóm bất biến, bám tiêu chí thành công của phase-04:

* **Đúng số**: snapshot khớp tổng hợp lại từ `gl_postings`; dư đầu kỳ lăn từ
  dư cuối kỳ trước; kỳ đầu năm lấy từ `opening_balances`; hai sổ độc lập.
* **Lùi ngày** (rủi ro SRS 19 §9 #6): chèn chứng từ vào kỳ đã tính → hàng đợi
  đúng một dấu; tính lại → mọi kỳ sau khớp.
* **Idempotent**: chạy hai lần liên tiếp cho kết quả giống hệt.
* **Không mất dấu bẩn**: lượt ghi sổ chen giữa lúc job đang tính làm mới
  `marked_at`, và `clear_marks` chỉ xóa phiên bản dấu nó đã đọc.

Mỗi test dựng **chi nhánh riêng** (fixture function-scope): recalc quét cả chi
nhánh, nên hai test chung chi nhánh sẽ nhìn thấy chứng từ của nhau.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import (
    JOURNAL_NUMBERING_RULE,
    JournalVoucherService,
)
from ket.posting.balances import recalc
from ket.posting.balances.models import AccountBalance, BalanceRecalcQueue
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.documents.service import VoucherDraft, VoucherService
from ket.posting.engine.models import Ledger
from ket.posting.engine.requests import PostingLine, PostingRequest
from ket.posting.engine.service import PostingService
from ket.posting.opening_balances.models import OpeningBalance
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"
JAN_15 = date(2026, 1, 15)
FEB_10 = date(2026, 2, 10)
MAR_05 = date(2026, 3, 5)


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


def _post_voucher(
    session: Session,
    context: PostingContext,
    *,
    posting_date: date,
    debit_account: str = "642",
    credit_account: str = "111",
    amount: int = 100_000,
) -> None:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=VND,
            exchange_rate=Decimal(1),
            description="chứng từ test recalc",
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


def _recalc_branch(session: Session, branch_id: int) -> recalc.RecalcOutcome:
    """Toàn bộ vòng của job, gọi thẳng phần lõi — job wrapper có test riêng."""
    marks = recalc.pending_marks(session, branch_id=branch_id)
    runs = recalc.build_runs(session, marks)
    done = 0
    for one_run in runs:
        for period in one_run.periods:
            recalc.recalc_period(session, ledger=one_run.ledger, branch_id=branch_id, period=period)
            done += 1
    cleared = recalc.clear_marks(session, branch_id=branch_id, marks=marks)
    return recalc.RecalcOutcome(periods_recalced=done, marks_cleared=cleared)


def _snapshot(
    session: Session, branch_id: int, *, ledger: int = Ledger.FINANCIAL
) -> list[tuple[object, ...]]:
    """Mọi dòng snapshot của một (sổ, chi nhánh), bỏ id/computed_at để so sánh."""
    rows = (
        session.execute(
            select(AccountBalance)
            .where(AccountBalance.branch_id == branch_id, AccountBalance.ledger == ledger)
            .order_by(
                AccountBalance.period_id, AccountBalance.account_id, AccountBalance.currency_code
            )
        )
        .scalars()
        .all()
    )
    return [
        (
            row.period_id,
            row.account_id,
            row.currency_code,
            row.opening_debit,
            row.opening_credit,
            row.period_debit,
            row.period_credit,
            row.closing_debit,
            row.closing_credit,
            row.opening_debit_fc,
            row.period_debit_fc,
        )
        for row in rows
    ]


def _period_of(session: Session, context: PostingContext, month: int) -> AccountingPeriod:
    return session.execute(
        select(AccountingPeriod).where(
            AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
            AccountingPeriod.period_no == month,
        )
    ).scalar_one()


def _balance_of(
    session: Session,
    context: PostingContext,
    *,
    month: int,
    account: str,
    ledger: int = Ledger.FINANCIAL,
    currency: str = VND,
) -> AccountBalance | None:
    period = _period_of(session, context, month)
    return session.execute(
        select(AccountBalance).where(
            AccountBalance.period_id == period.id,
            AccountBalance.ledger == ledger,
            AccountBalance.branch_id == context.branch_id,
            AccountBalance.account_id == context.accounts[account],
            AccountBalance.currency_code == currency,
        )
    ).scalar_one_or_none()


def test_recalc_rolls_balances_through_the_year(run: Runner, context: PostingContext) -> None:
    """Dư đầu lăn từ dư cuối kỳ trước; kỳ không phát sinh vẫn giữ số dư."""

    def work(session: Session) -> object:
        _post_voucher(session, context, posting_date=JAN_15, amount=100_000)
        _post_voucher(session, context, posting_date=FEB_10, amount=40_000)

        outcome = _recalc_branch(session, context.branch_id)
        # Hai sổ cùng bẩn từ kỳ 1 → mỗi sổ tính 12 kỳ của năm; bốn dấu bẩn
        # (2 kỳ × 2 sổ) đều được dọn vì dải kỳ từ kỳ 1 phủ hết.
        assert outcome.periods_recalced == 24
        assert outcome.marks_cleared == 4

        jan_cash = _balance_of(session, context, month=1, account="111")
        assert jan_cash is not None
        assert jan_cash.opening_debit == Decimal(0)
        assert jan_cash.period_credit == Decimal("100000.00")
        assert jan_cash.closing_credit == Decimal("100000.00")

        feb_cash = _balance_of(session, context, month=2, account="111")
        assert feb_cash is not None
        assert feb_cash.opening_credit == Decimal("100000.00")
        assert feb_cash.period_credit == Decimal("40000.00")
        assert feb_cash.closing_credit == Decimal("140000.00")

        # Kỳ 12 không phát sinh gì nhưng số dư vẫn phải lăn tới nơi.
        dec_cash = _balance_of(session, context, month=12, account="111")
        assert dec_cash is not None
        assert dec_cash.period_debit == Decimal(0)
        assert dec_cash.period_credit == Decimal(0)
        assert dec_cash.closing_credit == Decimal("140000.00")

        assert (
            session.execute(
                select(BalanceRecalcQueue).where(BalanceRecalcQueue.branch_id == context.branch_id)
            )
            .scalars()
            .first()
            is None
        )
        return None

    run(work)


def test_recalc_is_idempotent(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        _post_voucher(session, context, posting_date=JAN_15)
        _recalc_branch(session, context.branch_id)
        first = _snapshot(session, context.branch_id)
        assert first

        # Lượt hai không còn dấu bẩn → ép tính lại từ kỳ 1 và so từng dòng.
        period = _period_of(session, context, 1)
        for one_run in recalc.build_runs(
            session, (), forced=((Ledger.FINANCIAL.value, period.id),)
        ):
            for each in one_run.periods:
                recalc.recalc_period(
                    session, ledger=one_run.ledger, branch_id=context.branch_id, period=each
                )
        assert _snapshot(session, context.branch_id) == first
        return None

    run(work)


def test_backdated_voucher_marks_one_row_and_recalc_fixes_later_periods(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        _post_voucher(session, context, posting_date=FEB_10, amount=50_000)
        _recalc_branch(session, context.branch_id)

        # Chèn lùi ngày vào kỳ 1 — hàng đợi phải có đúng một dấu mỗi sổ.
        _post_voucher(session, context, posting_date=JAN_15, amount=70_000)
        marks = recalc.pending_marks(session, branch_id=context.branch_id)
        jan = _period_of(session, context, 1)
        assert [(mark.ledger, mark.from_period_id) for mark in marks] == [
            (Ledger.FINANCIAL.value, jan.id),
            (Ledger.MANAGEMENT.value, jan.id),
        ]

        _recalc_branch(session, context.branch_id)
        feb_cash = _balance_of(session, context, month=2, account="111")
        assert feb_cash is not None
        assert feb_cash.opening_credit == Decimal("70000.00")
        assert feb_cash.closing_credit == Decimal("120000.00")
        return None

    run(work)


def test_unpost_kills_orphan_snapshot_rows(run: Runner, context: PostingContext) -> None:
    """Khóa snapshot chỉ tồn tại nhờ chứng từ đã bỏ ghi sổ phải biến mất."""

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=MAR_05,
                posting_date=MAR_05,
                currency_code=VND,
                exchange_rate=Decimal(1),
                description="sẽ bị bỏ ghi sổ",
                # 112/642 chứ không 511: registry chiều mở rộng là dữ liệu DÙNG
                # CHUNG cả dataset, và một test khác đã khai chiều bắt buộc áp
                # lên TK 511 — validator #6 sẽ chặn dòng 511 không mang chiều.
                lines=(
                    JournalLineIn(account_id=context.accounts["112"], debit_fc=Decimal(30_000)),
                    JournalLineIn(account_id=context.accounts["642"], credit_fc=Decimal(30_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        _recalc_branch(session, context.branch_id)
        assert _balance_of(session, context, month=3, account="112") is not None

        service.unpost(voucher.id, user_id=ACTOR_ID)
        _recalc_branch(session, context.branch_id)
        assert _balance_of(session, context, month=3, account="112") is None
        assert _balance_of(session, context, month=3, account="642") is None
        return None

    run(work)


def test_first_period_opening_comes_from_opening_balances(
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
                debit=Decimal("500000.00"),
                debit_fc=Decimal("500000.00"),
                detail_kind=0,
            )
        )
        session.add(
            OpeningBalance(
                fiscal_year_id=context.fiscal_year_id,
                ledger=Ledger.FINANCIAL.value,
                branch_id=context.branch_id,
                account_id=context.accounts["331"],
                currency_code=VND,
                credit=Decimal("500000.00"),
                credit_fc=Decimal("500000.00"),
                detail_kind=3,
            )
        )
        session.flush()
        jan = _period_of(session, context, 1)
        mark_dirty(
            session,
            ledger=Ledger.FINANCIAL.value,
            branch_id=context.branch_id,
            from_period_id=jan.id,
            reason="nhập số dư đầu kỳ",
        )

        _recalc_branch(session, context.branch_id)
        jan_cash = _balance_of(session, context, month=1, account="111")
        assert jan_cash is not None
        assert jan_cash.opening_debit == Decimal("500000.00")
        assert jan_cash.opening_debit_fc == Decimal("500000.00")
        payable = _balance_of(session, context, month=1, account="331")
        assert payable is not None
        assert payable.opening_credit == Decimal("500000.00")
        assert payable.opening_debit_fc == Decimal("-500000.00")

        # Sổ quản trị không có số dư đầu → không một dòng nào (FR-OPB-008).
        assert (
            _balance_of(session, context, month=1, account="111", ledger=Ledger.MANAGEMENT) is None
        )
        return None

    run(work)


def test_management_ledger_snapshots_are_independent(run: Runner, context: PostingContext) -> None:
    """Dòng sổ quản trị riêng cho snapshot khác sổ tài chính (FR-NFR-031)."""

    def work(session: Session) -> object:
        voucher = VoucherService(session).create(
            VoucherDraft(
                document_type="GLE",
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code=VND,
                exchange_rate=Decimal(1),
            ),
            rule=JOURNAL_NUMBERING_RULE,
            user_id=ACTOR_ID,
        )

        def line(account: str, *, debit: int = 0, credit: int = 0) -> PostingLine:
            return PostingLine(
                account_id=context.accounts[account],
                debit_fc=Decimal(debit),
                credit_fc=Decimal(credit),
                currency=VND,
                rate=Decimal(1),
            )

        PostingService(session).post(
            PostingRequest(
                voucher_id=voucher.id,
                financial_lines=(
                    line("642", debit=80_000),
                    line("111", credit=80_000),
                ),
                management_lines=(
                    line("642", debit=50_000),
                    line("111", credit=50_000),
                ),
            ),
            user_id=ACTOR_ID,
        )
        _recalc_branch(session, context.branch_id)

        financial = _balance_of(session, context, month=1, account="111")
        management = _balance_of(session, context, month=1, account="111", ledger=Ledger.MANAGEMENT)
        assert financial is not None and management is not None
        assert financial.period_credit == Decimal("80000.00")
        assert management.period_credit == Decimal("50000.00")
        return None

    run(work)


def test_foreign_currency_net_rolls_across_periods(run: Runner, context: PostingContext) -> None:
    """Cột nguyên tệ ròng phải LĂN qua kỳ như số quy đổi (đột biến M03 review 4B).

    `opening_debit_fc` là đầu vào của đánh giá lại tỷ giá về sau; đứt chuỗi lăn
    (kỳ sau chép dư đầu kỳ trước thay vì cộng phát sinh) là kiểu sai chỉ lộ ra
    lúc lập bảng đánh giá lại cuối năm.
    """

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="USD",
                exchange_rate=Decimal(25_000),
                description="chứng từ USD",
                lines=(
                    JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(10)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(10)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        _recalc_branch(session, context.branch_id)

        jan_usd = _balance_of(session, context, month=1, account="111", currency="USD")
        assert jan_usd is not None
        assert jan_usd.opening_debit_fc == Decimal(0)
        assert jan_usd.period_debit_fc == Decimal("-10.00")

        # Kỳ 2: dư đầu nguyên tệ = dư đầu kỳ 1 + phát sinh ròng kỳ 1 — cả hai
        # chiều dấu (111 dư Có −10, 642 dư Nợ +10).
        feb_usd = _balance_of(session, context, month=2, account="111", currency="USD")
        assert feb_usd is not None
        assert feb_usd.opening_debit_fc == Decimal("-10.00")
        assert feb_usd.period_debit_fc == Decimal(0)
        feb_expense = _balance_of(session, context, month=2, account="642", currency="USD")
        assert feb_expense is not None
        assert feb_expense.opening_debit_fc == Decimal("10.00")
        return None

    run(work)


def test_refreshed_mark_survives_clear(run: Runner, context: PostingContext) -> None:
    """Dấu bẩn được làm mới sau khi job đã đọc thì không bị xóa oan."""

    def work(session: Session) -> object:
        _post_voucher(session, context, posting_date=JAN_15)
        marks = recalc.pending_marks(session, branch_id=context.branch_id)
        assert marks

        # Một lượt ghi sổ chen ngang: cùng khóa, `marked_at` mới.
        jan = _period_of(session, context, 1)
        mark_dirty(
            session,
            ledger=Ledger.FINANCIAL.value,
            branch_id=context.branch_id,
            from_period_id=jan.id,
            reason="ghi sổ chen ngang",
        )

        cleared = recalc.clear_marks(session, branch_id=context.branch_id, marks=marks)
        # Dấu sổ tài chính vừa làm mới phải sống; dấu sổ quản trị bị xóa.
        assert cleared == 1
        remaining = recalc.pending_marks(session, branch_id=context.branch_id)
        assert [(mark.ledger, mark.from_period_id) for mark in remaining] == [
            (Ledger.FINANCIAL.value, jan.id)
        ]
        return None

    run(work)


def test_marks_across_two_ledgers_merge_to_earliest_period(
    run: Runner, context: PostingContext
) -> None:
    """Nhiều dấu cùng (sổ, năm) gộp về kỳ sớm nhất — một dải kỳ duy nhất."""

    def work(session: Session) -> object:
        _post_voucher(session, context, posting_date=FEB_10)
        _post_voucher(session, context, posting_date=JAN_15)
        marks = recalc.pending_marks(session, branch_id=context.branch_id)
        # Hai kỳ × hai sổ = 4 dấu, nhưng chỉ 2 dải (mỗi sổ một dải từ kỳ 1).
        assert len(marks) == 4
        runs = recalc.build_runs(session, marks)
        assert len(runs) == 2
        jan = _period_of(session, context, 1)
        for one_run in runs:
            assert one_run.periods[0].id == jan.id
            assert len(one_run.periods) == 12
        return None

    run(work)
