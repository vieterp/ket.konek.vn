"""Xóa trọn một nhóm số dư ban đầu (M5, lát 4E) — job `clear_group`.

Ngữ nghĩa nhập giữ nguyên an toàn (sheet vắng = không đụng tới — review 4C);
job này là đường duy nhất đưa một nhóm về 0. Bất biến kiểm ở đây:

* chỉ xóa đúng (năm, sổ, chi nhánh, nhóm) — nhóm khác, sổ khác còn nguyên;
* chi tiết hóa đơn đi theo cha (ON DELETE CASCADE);
* xóa xong đánh dấu bẩn kỳ đầu năm (bàn giao 4B) + nhật ký kèm số dòng;
* kỳ đầu năm đã khóa → từ chối (cùng rào với import/carry-forward);
* nhóm 5–9 bị chặn từ tham số (bảng chi tiết của chúng thuộc phase 8);
* chạy lại lần hai xóa 0 dòng — idempotent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import OpeningBalanceSettledError, OpeningPeriodLockedError
from ket.kernel.jobs.registry import JobContext
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.clear_job import ClearGroupParams, run_clear_group
from ket.posting.opening_balances.guards import acquire_opening_write_lock
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


def _seed_two_groups(session: Session, context: PostingContext) -> int:
    """Nhóm 0 (TK thường) + nhóm 3 (phải trả, có hóa đơn) trên cả hai sổ.

    Trả về `id` dòng nhóm 3 sổ tài chính — dòng mang hóa đơn cascade.
    """
    payable_id = 0
    for ledger in (Ledger.FINANCIAL.value, Ledger.MANAGEMENT.value):
        account_row = OpeningBalance(
            fiscal_year_id=context.fiscal_year_id,
            ledger=ledger,
            branch_id=context.branch_id,
            account_id=context.accounts["111"],
            currency_code=VND,
            debit=Decimal("500000.00"),
            debit_fc=Decimal("500000.00"),
            detail_kind=OpeningDetailKind.ACCOUNT,
        )
        payable_row = OpeningBalance(
            fiscal_year_id=context.fiscal_year_id,
            ledger=ledger,
            branch_id=context.branch_id,
            account_id=context.accounts["331"],
            currency_code=VND,
            credit=Decimal("500000.00"),
            credit_fc=Decimal("500000.00"),
            detail_kind=OpeningDetailKind.PAYABLE,
        )
        session.add_all([account_row, payable_row])
        session.flush()
        session.add(
            OpeningBalanceInvoice(
                opening_balance_id=payable_row.id,
                branch_id=payable_row.branch_id,
                invoice_no=f"HD-{ledger}",
                invoice_date=date(2025, 12, 1),
                amount=Decimal("500000.00"),
                amount_fc=Decimal("500000.00"),
            )
        )
        if ledger == Ledger.FINANCIAL.value:
            payable_id = payable_row.id
    session.flush()
    return payable_id


def _clear(
    session: Session,
    dataset: DatasetRef,
    context: PostingContext,
    *,
    detail_kind: int,
    ledger: int = Ledger.FINANCIAL.value,
) -> dict[str, object]:
    job_context = JobContext(
        job_id=uuid4(),
        session=session,
        progress=FakeProgress(reports=[]),
        attempt=1,
        dataset_schema=dataset.schema_name,
        branch_id=context.branch_id,
        requested_by=ACTOR_ID,
    )
    result = run_clear_group(
        job_context,
        ClearGroupParams(
            fiscal_year_id=context.fiscal_year_id, ledger=ledger, detail_kind=detail_kind
        ),
    )
    assert result is not None
    return dict(result)


def _rows_of(
    session: Session, context: PostingContext, *, ledger: int, detail_kind: int
) -> list[OpeningBalance]:
    return list(
        session.execute(
            select(OpeningBalance)
            .where(OpeningBalance.fiscal_year_id == context.fiscal_year_id)
            .where(OpeningBalance.ledger == ledger)
            .where(OpeningBalance.branch_id == context.branch_id)
            .where(OpeningBalance.detail_kind == detail_kind)
        ).scalars()
    )


def test_clear_removes_only_the_targeted_group_and_cascades_invoices(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        payable_id = _seed_two_groups(session, context)

    with unit_of_work(session_factory, scope) as session:
        result = _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.PAYABLE)
        assert result["deleted_rows"] == 1

    with unit_of_work(session_factory, scope) as session:
        # Nhóm 3 sổ tài chính sạch; hóa đơn treo dưới nó đi theo.
        assert (
            _rows_of(
                session,
                context,
                ledger=Ledger.FINANCIAL.value,
                detail_kind=OpeningDetailKind.PAYABLE,
            )
            == []
        )
        orphan = (
            session.execute(
                select(OpeningBalanceInvoice).where(
                    OpeningBalanceInvoice.opening_balance_id == payable_id
                )
            )
            .scalars()
            .all()
        )
        assert orphan == []
        # Nhóm 0 cùng sổ và nhóm 3 sổ quản trị còn nguyên — xóa không lan.
        assert (
            len(
                _rows_of(
                    session,
                    context,
                    ledger=Ledger.FINANCIAL.value,
                    detail_kind=OpeningDetailKind.ACCOUNT,
                )
            )
            == 1
        )
        assert (
            len(
                _rows_of(
                    session,
                    context,
                    ledger=Ledger.MANAGEMENT.value,
                    detail_kind=OpeningDetailKind.PAYABLE,
                )
            )
            == 1
        )
        # Nhật ký DELETED kèm số dòng đã xóa — cổng canh cho `record_action`
        # (mutation review 4E: gỡ audit khỏi job phải làm test này đỏ).
        audit = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "opening_balances")
                .where(AuditLog.action == AuditAction.DELETED.value)
                .where(AuditLog.branch_id == context.branch_id)
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        assert audit is not None
        assert audit.new_values is not None
        assert audit.new_values["deleted_rows"] == 1
        assert audit.new_values["detail_kind"] == int(OpeningDetailKind.PAYABLE)


def test_a_writer_holding_the_opening_lock_serialises_the_clear(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Advisory lock của `clear_group` là thật, không phải trang trí (RT-09).

    Phiên A cầm đúng khóa mà mọi đường ghi số dư cầm (`acquire_opening_write_lock`
    — cùng tag (schema, chi nhánh, năm)); phiên B chạy lượt xóa với
    `lock_timeout` ngắn. Job còn khóa → B phải chờ và vấp timeout; gỡ
    `acquire_opening_write_lock` khỏi `clear_job` thì B xóa xong ngay và test
    này đỏ — giết mutation sống sót của review 4E (họ survivor RT-09 lặp lại
    ở 4A/4C).
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _seed_two_groups(session, context)

    with unit_of_work(session_factory, scope) as holder:
        acquire_opening_write_lock(
            holder,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=context.branch_id,
            fiscal_year_id=context.fiscal_year_id,
        )
        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, scope) as blocked:
                blocked.execute(text("SET LOCAL lock_timeout = '500ms'"))
                _clear(
                    session=blocked,
                    dataset=dataset_alpha,
                    context=context,
                    detail_kind=OpeningDetailKind.ACCOUNT,
                )

    # Khóa đã nhả theo transaction — lượt xóa bình thường phải chạy được ngay.
    with unit_of_work(session_factory, scope) as session:
        result = _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.ACCOUNT)
        assert result["deleted_rows"] == 1


def test_clear_marks_the_first_period_dirty_for_its_ledger(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _seed_two_groups(session, context)
        # Dọn dấu bẩn sẵn có để phép đo dưới quy về đúng lượt xóa.
        for mark in session.execute(select(BalanceRecalcQueue)).scalars().all():
            session.delete(mark)

    with unit_of_work(session_factory, scope) as session:
        _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.ACCOUNT)

    with unit_of_work(session_factory, scope) as session:
        first_period_id = session.execute(
            select(AccountingPeriod.id)
            .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
            .order_by(AccountingPeriod.period_no)
            .limit(1)
        ).scalar_one()
        marks = (
            session.execute(
                select(BalanceRecalcQueue).where(
                    BalanceRecalcQueue.branch_id == context.branch_id,
                    BalanceRecalcQueue.from_period_id == first_period_id,
                    BalanceRecalcQueue.ledger == Ledger.FINANCIAL.value,
                )
            )
            .scalars()
            .all()
        )
        assert len(marks) == 1


def test_clearing_twice_is_idempotent(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _seed_two_groups(session, context)

    with unit_of_work(session_factory, scope) as session:
        first = _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.PAYABLE)
    with unit_of_work(session_factory, scope) as session:
        second = _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.PAYABLE)
    assert first["deleted_rows"] == 1
    assert second["deleted_rows"] == 0


def test_locked_first_period_blocks_the_clear(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _seed_two_groups(session, context)
        first_period = session.execute(
            select(AccountingPeriod)
            .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
            .order_by(AccountingPeriod.period_no)
            .limit(1)
        ).scalar_one()
        first_period.locked_at = datetime.now(tz=UTC)
        first_period.locked_by = ACTOR_ID

    try:
        with unit_of_work(session_factory, scope) as session:
            with pytest.raises(OpeningPeriodLockedError):
                _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.ACCOUNT)
    finally:
        with unit_of_work(session_factory, scope) as session:
            period = session.execute(
                select(AccountingPeriod)
                .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
                .order_by(AccountingPeriod.period_no)
                .limit(1)
            ).scalar_one()
            period.locked_at = None
            period.locked_by = None


def test_phase_8_kinds_are_rejected_at_the_parameter(context: PostingContext) -> None:
    with pytest.raises(ValidationError):
        ClearGroupParams(
            fiscal_year_id=context.fiscal_year_id,
            ledger=Ledger.FINANCIAL.value,
            detail_kind=OpeningDetailKind.STOCK,
        )


def test_clear_refuses_a_group_with_settled_invoices(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Hóa đơn đã bị phiếu đối trừ (paid_amount_fc > 0) thì không xóa nhóm được
    (review 6B, M-1): xóa đích làm phiếu đã ghi sổ kẹt vĩnh viễn — bỏ ghi sổ
    không còn dòng nào để gỡ số đã trả."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        payable_id = _seed_two_groups(session, context)
        invoice = session.execute(
            select(OpeningBalanceInvoice).where(
                OpeningBalanceInvoice.opening_balance_id == payable_id
            )
        ).scalar_one()
        invoice.paid_amount_fc = Decimal("100000.00")
        invoice.paid_amount = Decimal("100000.00")
        session.flush()

        with pytest.raises(OpeningBalanceSettledError):
            _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.PAYABLE)

        # Gỡ đối trừ (như bỏ ghi sổ phiếu) thì xóa lại được.
        invoice.paid_amount_fc = Decimal(0)
        invoice.paid_amount = Decimal(0)
        session.flush()
        result = _clear(session, dataset_alpha, context, detail_kind=OpeningDetailKind.PAYABLE)
        assert result["deleted_rows"] == 1
