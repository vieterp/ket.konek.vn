"""Cổng khóa / mở kỳ (lát 4D, bước 14) — luật nghiệp vụ trên PostgreSQL thật.

Mỗi nhóm test dùng một NĂM TÀI CHÍNH riêng (mã riêng, không phải năm 2026 dùng
chung của `posting_support`): khóa kỳ là thao tác toàn-dữ-liệu, và khóa năm
dùng chung sẽ chặn đường ghi sổ của mọi tệp test chạy sau. Phần đua hai kết
nối (FOR SHARE ↔ FOR UPDATE) nằm ở `test_lock_vs_post_writeskew.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    PeriodClosedError,
    PeriodLockChecklistError,
    PeriodLockScopeError,
    PeriodLockSequenceError,
    PeriodNotFoundError,
    PeriodNotLockedError,
    PeriodRecalcPendingError,
)
from ket.kernel.periods.models import (
    AccountingPeriod,
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.opening_balances.models import OpeningBalance
from ket.posting.periods.lock_service import PeriodLockService
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


def full_scope(
    session_factory: sessionmaker[Session], dataset: DatasetRef, context: PostingContext
) -> RequestScope:
    """Phạm vi phủ MỌI chi nhánh của dataset — điều kiện cần để khóa kỳ.

    Đọc danh sách chi nhánh bằng scope hẹp trước (bảng `branches` không bật
    RLS nên thấy đủ), rồi dựng scope rộng.
    """
    with unit_of_work(
        session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
    ) as session:
        branch_ids = tuple(sorted(session.execute(select(Branch.id)).scalars().all()))
    return RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=ACTOR_ID,
        branch_ids=branch_ids,
        acting_branch_id=context.branch_id,
    )


Runner = Callable[[Callable[[Session, RequestScope], object]], object]


@pytest.fixture
def run_wide(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> Runner:
    """Chạy một hàm nghiệp vụ dưới phạm vi mọi-chi-nhánh, đưa kèm scope đó."""

    def runner(work: Callable[[Session, RequestScope], object]) -> object:
        scope = full_scope(session_factory, dataset_alpha, context)
        with unit_of_work(session_factory, scope) as session:
            return work(session, scope)

    return runner


def make_year(session: Session, *, code: str, start: date) -> int:
    """Một niên độ riêng cho một nhóm test — không đụng năm 2026 dùng chung."""
    existing = session.scalar(select(FiscalYear.id).where(FiscalYear.code == code))
    if existing is not None:
        return existing
    year = PeriodService(session).create_fiscal_year(
        code=code,
        start_date=start,
        accounting_scheme=AccountingScheme.TT99,
        base_currency="VND",
        inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
        vat_method=VatMethod.DEDUCTION,
    )
    return year.id


def period_of(session: Session, year_id: int, period_no: int) -> AccountingPeriod:
    return session.execute(
        select(AccountingPeriod)
        .where(AccountingPeriod.fiscal_year_id == year_id)
        .where(AccountingPeriod.period_no == period_no)
    ).scalar_one()


def post_voucher(
    session: Session, context: PostingContext, *, posting_date: date, post: bool = True
) -> str:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="chứng từ test khóa kỳ",
            lines=(
                JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(50_000)),
                JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(50_000)),
            ),
        ),
        user_id=ACTOR_ID,
    )
    if post:
        service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.voucher_no


def clear_queue_of_year(session: Session, year_id: int) -> None:
    """Giả lập "đã tính lại xong": xóa dấu bẩn trỏ vào kỳ của năm này.

    Đường thật là job recalc (đã có test riêng ở `test_balance_recalc_job.py`);
    ở đây chỉ cần trạng thái "hàng đợi rỗng" để đi tiếp luật khóa.
    """
    period_ids = select(AccountingPeriod.id).where(AccountingPeriod.fiscal_year_id == year_id)
    session.execute(
        sa_delete(BalanceRecalcQueue).where(BalanceRecalcQueue.from_period_id.in_(period_ids))
    )


class TestLock:
    def test_lock_requires_scope_covering_every_branch(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        """Scope một chi nhánh không khóa được — kể cả khi mọi phép kiểm khác xanh.

        `seed_posting_context` của các tệp test khác bảo đảm dataset có nhiều
        chi nhánh hơn một; nếu module này chạy một mình thì chi nhánh thứ hai
        được tạo ngay trong test.
        """
        narrow = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, narrow) as session:
            total_branches = len(session.execute(select(Branch.id)).scalars().all())
            if total_branches < 2:  # pragma: no cover - phụ thuộc thứ tự chạy
                seed_posting_context(session_factory, dataset_alpha)
            year_id = make_year(session, code="LOCK-SCOPE", start=date(2070, 1, 1))
            period = period_of(session, year_id, 1)
            with pytest.raises(PeriodLockScopeError) as caught:
                PeriodLockService(session).lock(period.id, scope=narrow)
            # ĐẾM, không id (review pre-landing 6G-2 M-1): ba cửa hỏi cùng câu
            # hỏi bằng cùng `missing_scope_branch_ids` — khóa sổ, đối chiếu
            # ngân hàng, báo cáo phạm vi công ty — nên cả ba phải trả cùng hình
            # dạng. Id chi nhánh người gọi chưa được cấp là thông tin về cấu
            # trúc đơn vị mà họ chưa được thấy; nó đi thẳng ra thân phản hồi.
            details = caught.value.details
            assert "missing_branch_ids" not in details, details
            assert details["branches_visible"] < details["branches_total"]

    def test_lock_must_be_sequential_within_the_year(self, run_wide: Runner) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-SEQ", start=date(2071, 1, 1))
            period_2 = period_of(session, year_id, 2)
            with pytest.raises(PeriodLockSequenceError) as caught:
                PeriodLockService(session).lock(period_2.id, scope=scope)
            assert caught.value.details["open_period_nos"] == "1"
            return None

        run_wide(work)

    def test_dirty_recalc_queue_blocks_the_lock_until_cleared(
        self, run_wide: Runner, context: PostingContext
    ) -> None:
        """Ghi sổ đánh dấu kỳ bẩn → khóa bị chặn; hàng đợi rỗng → khóa được,
        kèm dòng audit LOCKED (FR-GLE-030)."""

        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-Q", start=date(2072, 1, 1))
            post_voucher(session, context, posting_date=date(2072, 1, 10))
            period_1 = period_of(session, year_id, 1)
            service = PeriodLockService(session)

            with pytest.raises(PeriodRecalcPendingError) as caught:
                service.lock(period_1.id, scope=scope)
            assert str(context.branch_id) in str(caught.value.details["branch_ids"])

            clear_queue_of_year(session, year_id)
            outcome = service.lock(period_1.id, scope=scope)
            assert outcome.period.locked_at is not None
            assert outcome.period.locked_by == ACTOR_ID
            assert outcome.warnings == ()

            audit = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "accounting_periods")
                    .where(AuditLog.entity_id == str(period_1.id))
                    .where(AuditLog.action == AuditAction.LOCKED.value)
                )
                .scalars()
                .all()
            )
            assert len(audit) == 1
            assert audit[0].new_values is not None
            assert audit[0].new_values["period_no"] == 1
            return None

        run_wide(work)

    def test_dirty_mark_in_a_later_period_does_not_block(
        self, run_wide: Runner, context: PostingContext
    ) -> None:
        """Dấu bẩn kỳ 3 không chặn khóa kỳ 1 — recalc từ kỳ 3 không viết lại kỳ 1."""

        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-LATER", start=date(2073, 1, 1))
            post_voucher(session, context, posting_date=date(2073, 3, 10))
            period_1 = period_of(session, year_id, 1)
            outcome = PeriodLockService(session).lock(period_1.id, scope=scope)
            assert outcome.period.locked_at is not None
            return None

        run_wide(work)

    def test_unposted_vouchers_warn_but_do_not_block(
        self, run_wide: Runner, context: PostingContext
    ) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-WARN", start=date(2074, 1, 1))
            voucher_no = post_voucher(session, context, posting_date=date(2074, 1, 20), post=False)
            period_1 = period_of(session, year_id, 1)
            outcome = PeriodLockService(session).lock(period_1.id, scope=scope)
            assert outcome.period.locked_at is not None
            assert len(outcome.warnings) == 1
            warning = outcome.warnings[0]
            assert warning.code == "vouchers.unposted"
            assert warning.count == 1
            assert voucher_no in warning.sample
            return None

        run_wide(work)

    def test_unbalanced_opening_balances_block_locking_the_first_period(
        self,
        run_wide: Runner,
        context: PostingContext,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
    ) -> None:
        """Mục chặn U11 của phase 4: lệch số dư ban đầu → kỳ đầu năm không khóa
        được; cân lại → khóa được. Kỳ sau không kiểm lại (số dư đã bất động).

        Cân là chuyện **từng chi nhánh** (review 4D, H-2): một dòng Có ở chi
        nhánh KHÁC bù trừ được tổng gộp nhưng không được phép mở cổng — hai
        nhóm per-branch vẫn lệch, và khóa xong là số dư đóng băng với integrity
        đỏ vĩnh viễn.
        """

        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-OPB", start=date(2075, 1, 1))
            session.add(
                OpeningBalance(
                    fiscal_year_id=year_id,
                    ledger=0,
                    branch_id=context.branch_id,
                    account_id=context.accounts["111"],
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    debit=Decimal(700_000),
                    credit=Decimal(0),
                    debit_fc=Decimal(700_000),
                    credit_fc=Decimal(0),
                    detail_kind=0,
                )
            )
            session.flush()
            period_1 = period_of(session, year_id, 1)
            service = PeriodLockService(session)

            with pytest.raises(PeriodLockChecklistError) as caught:
                service.lock(period_1.id, scope=scope)
            assert caught.value.details["check"] == "opening_balance_balanced"

            # Bù trừ CHÉO chi nhánh: tổng gộp cân nhưng từng chi nhánh vẫn lệch
            # → cổng phải tiếp tục chặn (H-2).
            other_branch_id = session.execute(
                select(Branch.id).where(Branch.id != context.branch_id).limit(1)
            ).scalar_one()
            cross_branch = OpeningBalance(
                fiscal_year_id=year_id,
                ledger=0,
                branch_id=other_branch_id,
                account_id=context.accounts["131"],
                currency_code="VND",
                exchange_rate=Decimal(1),
                debit=Decimal(0),
                credit=Decimal(700_000),
                debit_fc=Decimal(0),
                credit_fc=Decimal(700_000),
                detail_kind=0,
            )
            session.add(cross_branch)
            session.flush()
            with pytest.raises(PeriodLockChecklistError) as still:
                service.lock(period_1.id, scope=scope)
            assert still.value.details["check"] == "opening_balance_balanced"
            session.execute(sa_delete(OpeningBalance).where(OpeningBalance.id == cross_branch.id))

            session.add(
                OpeningBalance(
                    fiscal_year_id=year_id,
                    ledger=0,
                    branch_id=context.branch_id,
                    account_id=context.accounts["131"],
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    debit=Decimal(0),
                    credit=Decimal(700_000),
                    debit_fc=Decimal(0),
                    credit_fc=Decimal(700_000),
                    detail_kind=0,
                )
            )
            session.flush()
            outcome = service.lock(period_1.id, scope=scope)
            assert outcome.period.locked_at is not None
            return None

        # Chi nhánh thứ hai phải TỒN TẠI TRƯỚC khi `run_wide` dựng scope —
        # dòng bù-chéo cần nó, và scope thiếu nó thì RLS WITH CHECK chặn INSERT.
        narrow = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, narrow) as session:
            has_other = session.execute(
                select(Branch.id).where(Branch.id != context.branch_id).limit(1)
            ).scalar_one_or_none()
        if has_other is None:  # pragma: no cover - chỉ khi chạy lẻ tệp này
            seed_posting_context(session_factory, dataset_alpha)
        run_wide(work)

    def test_locking_twice_and_unknown_period_are_refused(self, run_wide: Runner) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-DUP", start=date(2076, 1, 1))
            period_1 = period_of(session, year_id, 1)
            service = PeriodLockService(session)
            service.lock(period_1.id, scope=scope)
            with pytest.raises(PeriodClosedError):
                service.lock(period_1.id, scope=scope)
            with pytest.raises(PeriodNotFoundError):
                service.lock(999_999_999, scope=scope)
            return None

        run_wide(work)


class TestUnlock:
    def test_unlock_requires_a_locked_period_and_goes_reverse_sequential(
        self, run_wide: Runner
    ) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="UNLOCK-SEQ", start=date(2077, 1, 1))
            service = PeriodLockService(session)
            period_1 = period_of(session, year_id, 1)
            period_2 = period_of(session, year_id, 2)

            with pytest.raises(PeriodNotLockedError):
                service.unlock(period_1.id, scope=scope, reason="chưa khóa mà mở")

            service.lock(period_1.id, scope=scope)
            service.lock(period_2.id, scope=scope)

            # Kỳ 2 còn khóa → kỳ 1 không mở được (mở phải từ kỳ muộn nhất).
            with pytest.raises(PeriodLockSequenceError):
                service.unlock(period_1.id, scope=scope, reason="mở sai thứ tự")

            unlocked = service.unlock(period_2.id, scope=scope, reason="bổ sung chứng từ")
            assert unlocked.locked_at is None

            audit = (
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "accounting_periods")
                    .where(AuditLog.entity_id == str(period_2.id))
                    .where(AuditLog.action == AuditAction.UNLOCKED.value)
                )
                .scalars()
                .all()
            )
            assert len(audit) == 1
            assert audit[0].new_values is not None
            assert audit[0].new_values["reason"] == "bổ sung chứng từ"
            return None

        run_wide(work)

    def test_blank_reason_is_a_programming_error(self, run_wide: Runner) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="UNLOCK-REASON", start=date(2078, 1, 1))
            period_1 = period_of(session, year_id, 1)
            with pytest.raises(ValueError, match="Lý do"):
                PeriodLockService(session).unlock(period_1.id, scope=scope, reason="   ")
            return None

        run_wide(work)

    def test_closed_year_blocks_both_lock_and_unlock(self, run_wide: Runner) -> None:
        def work(session: Session, scope: RequestScope) -> object:
            year_id = make_year(session, code="LOCK-CLOSED", start=date(2079, 1, 1))
            service = PeriodLockService(session)
            period_1 = period_of(session, year_id, 1)
            service.lock(period_1.id, scope=scope)

            year = session.get(FiscalYear, year_id)
            assert year is not None
            year.is_closed = True
            session.flush()

            with pytest.raises(PeriodClosedError):
                service.lock(period_of(session, year_id, 2).id, scope=scope)
            with pytest.raises(PeriodClosedError):
                service.unlock(period_1.id, scope=scope, reason="năm đã quyết toán")
            return None

        run_wide(work)
