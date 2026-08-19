"""Chuyển số dư sang năm sau trên PostgreSQL thật (slice 4C, bước 13 — FR-OPB-010).

Bất biến chính, bám tiêu chí phase-04:

* Số chuyển sang = số dư ban đầu năm nguồn + phát sinh đã ghi sổ, **giữ nguyên
  chiều chi tiết** (đối tượng công nợ) — tính thẳng từ `gl_postings`, không
  phụ thuộc snapshot sạch hay bẩn.
* Chi tiết hóa đơn còn nợ đi theo (tiêu chí "Chuyển số dư sang năm sau giữ
  nguyên chi tiết hóa đơn AR/AP").
* Chạy lại phải có xác nhận ghi đè; với ghi đè thì idempotent.
* Ghi xong tự `mark_dirty` kỳ đầu năm nhận (bàn giao 4B).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    OpeningCarryForwardExistsError,
    OpeningCarryForwardTargetMissingError,
)
from ket.kernel.jobs.registry import JobContext
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.periods.models import (
    AccountingPeriod,
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.engine.dimensions import PartnerKind
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.carry_forward_job import (
    CarryForwardParams,
    run_carry_forward,
)
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


def ensure_fiscal_year(session: Session, code: str, start: date) -> int:
    existing = session.scalar(select(FiscalYear.id).where(FiscalYear.code == code))
    if existing is not None:
        return existing
    year = PeriodService(session).create_fiscal_year(
        code=code,
        start_date=start,
        accounting_scheme=AccountingScheme.TT99,
        base_currency=VND,
        inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
        vat_method=VatMethod.DEDUCTION,
    )
    return year.id


def carry_forward(
    session: Session,
    dataset: DatasetRef,
    context: PostingContext,
    *,
    from_fiscal_year_id: int | None = None,
    overwrite: bool = False,
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
    result = run_carry_forward(
        job_context,
        CarryForwardParams(
            from_fiscal_year_id=(
                from_fiscal_year_id if from_fiscal_year_id is not None else context.fiscal_year_id
            ),
            overwrite=overwrite,
        ),
    )
    assert result is not None
    return dict(result)


def seed_source_year(session: Session, context: PostingContext, *, vendor_id: int) -> None:
    """Số dư 2026: 111 nợ 1.000.000 ↔ 331 (NCC) có 1.000.000, hai hóa đơn; cộng
    một khoản trả nợ 400.000 trong năm (ghi sổ thật qua posting engine)."""
    parent = OpeningBalance(
        fiscal_year_id=context.fiscal_year_id,
        ledger=Ledger.FINANCIAL.value,
        branch_id=context.branch_id,
        account_id=context.accounts["331"],
        currency_code=VND,
        partner_id=vendor_id,
        partner_kind=PartnerKind.VENDOR.value,
        credit=Decimal("1000000.00"),
        credit_fc=Decimal("1000000.00"),
        detail_kind=OpeningDetailKind.PAYABLE,
    )
    cash = OpeningBalance(
        fiscal_year_id=context.fiscal_year_id,
        ledger=Ledger.FINANCIAL.value,
        branch_id=context.branch_id,
        account_id=context.accounts["111"],
        currency_code=VND,
        debit=Decimal("1000000.00"),
        debit_fc=Decimal("1000000.00"),
        detail_kind=OpeningDetailKind.ACCOUNT,
    )
    session.add_all([parent, cash])
    session.flush()
    session.add_all(
        [
            OpeningBalanceInvoice(
                opening_balance_id=parent.id,
                invoice_no="HD-A",
                invoice_date=date(2025, 11, 20),
                amount=Decimal("600000.00"),
                amount_fc=Decimal("600000.00"),
            ),
            OpeningBalanceInvoice(
                opening_balance_id=parent.id,
                invoice_no="HD-B",
                invoice_date=date(2025, 12, 5),
                amount=Decimal("400000.00"),
                amount_fc=Decimal("400000.00"),
            ),
        ]
    )
    session.flush()

    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=date(2026, 3, 10),
            posting_date=date(2026, 3, 10),
            currency_code=VND,
            exchange_rate=Decimal(1),
            description="trả bớt nợ NCC",
            lines=(
                JournalLineIn(
                    account_id=context.accounts["331"],
                    debit_fc=Decimal(400_000),
                    partner_id=vendor_id,
                    partner_kind=PartnerKind.VENDOR,
                ),
                JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(400_000)),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)


def target_rows(
    session: Session, context: PostingContext, target_year_id: int, *, ledger: int
) -> list[OpeningBalance]:
    return list(
        session.execute(
            select(OpeningBalance)
            .where(OpeningBalance.fiscal_year_id == target_year_id)
            .where(OpeningBalance.branch_id == context.branch_id)
            .where(OpeningBalance.ledger == ledger)
            .order_by(OpeningBalance.detail_kind, OpeningBalance.id)
        )
        .scalars()
        .all()
    )


def test_carry_forward_moves_closing_balances_and_invoice_detail(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        vendor = MasterDataService(session, Partner).create(
            code=f"NCC-CF-{context.branch_code}", name="NCC carry", extra={"is_vendor": True}
        )
        vendor_id = vendor.id
        target_year_id = ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        seed_source_year(session, context, vendor_id=vendor_id)

        result = carry_forward(session, dataset_alpha, context)
        assert result["target_fiscal_year_id"] == target_year_id
        assert result["invoices_carried"] == 2

        rows = target_rows(session, context, target_year_id, ledger=Ledger.FINANCIAL)
        by_kind = {row.detail_kind: row for row in rows}
        # 111: 1.000.000 − 400.000 = 600.000; 331 NCC: 1.000.000 − 400.000.
        assert by_kind[OpeningDetailKind.ACCOUNT].debit == Decimal("600000.00")
        payable = by_kind[OpeningDetailKind.PAYABLE]
        assert payable.credit == Decimal("600000.00")
        assert payable.partner_id == vendor_id
        assert payable.partner_kind == PartnerKind.VENDOR.value

        invoices = list(
            session.execute(
                select(OpeningBalanceInvoice)
                .where(OpeningBalanceInvoice.opening_balance_id == payable.id)
                .order_by(OpeningBalanceInvoice.invoice_no)
            )
            .scalars()
            .all()
        )
        # `paid_amount` chưa sống tới phase 7 nên cả hai hóa đơn còn nguyên.
        assert [(invoice.invoice_no, invoice.amount) for invoice in invoices] == [
            ("HD-A", Decimal("600000.00")),
            ("HD-B", Decimal("400000.00")),
        ]

        # Sổ quản trị nhận bản sao phát sinh (engine nhân bản) nhưng không có
        # số dư ban đầu tài chính — hai sổ độc lập cả ở lượt chuyển.
        management = target_rows(session, context, target_year_id, ledger=Ledger.MANAGEMENT)
        management_by_kind = {row.detail_kind: row for row in management}
        assert management_by_kind[OpeningDetailKind.PAYABLE].debit == Decimal("400000.00")

        # Bàn giao 4B: kỳ đầu năm nhận được đánh dấu bẩn cho CẢ hai sổ.
        target_first_period = session.execute(
            select(AccountingPeriod.id)
            .where(AccountingPeriod.fiscal_year_id == target_year_id)
            .order_by(AccountingPeriod.period_no)
            .limit(1)
        ).scalar_one()
        marks = (
            session.execute(
                select(BalanceRecalcQueue.ledger)
                .where(BalanceRecalcQueue.branch_id == context.branch_id)
                .where(BalanceRecalcQueue.from_period_id == target_first_period)
            )
            .scalars()
            .all()
        )
        assert sorted(marks) == [Ledger.FINANCIAL.value, Ledger.MANAGEMENT.value]


def test_carry_forward_requires_explicit_overwrite_and_is_idempotent(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        vendor = MasterDataService(session, Partner).create(
            code=f"NCC-CF2-{context.branch_code}", name="NCC carry 2", extra={"is_vendor": True}
        )
        target_year_id = ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        seed_source_year(session, context, vendor_id=vendor.id)

        first = carry_forward(session, dataset_alpha, context)
        with pytest.raises(OpeningCarryForwardExistsError):
            carry_forward(session, dataset_alpha, context)
        second = carry_forward(session, dataset_alpha, context, overwrite=True)
        assert first["rows_financial"] == second["rows_financial"]

        rows = target_rows(session, context, target_year_id, ledger=Ledger.FINANCIAL)
        # Ghi đè thay trọn chứ không cộng dồn: vẫn đúng hai dòng.
        assert len(rows) == 2


def test_carry_forward_without_next_year_is_rejected(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        lonely_year = ensure_fiscal_year(session, "2098", date(2098, 1, 1))
        session.add(
            OpeningBalance(
                fiscal_year_id=lonely_year,
                ledger=Ledger.FINANCIAL.value,
                branch_id=context.branch_id,
                account_id=context.accounts["111"],
                currency_code=VND,
                debit=Decimal("1.00"),
                debit_fc=Decimal("1.00"),
                detail_kind=OpeningDetailKind.ACCOUNT,
            )
        )
        session.flush()
        with pytest.raises(OpeningCarryForwardTargetMissingError):
            carry_forward(session, dataset_alpha, context, from_fiscal_year_id=lonely_year)


def test_carry_forward_preserves_foreign_currency_detail(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        target_year_id = ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        session.add(
            OpeningBalance(
                fiscal_year_id=context.fiscal_year_id,
                ledger=Ledger.FINANCIAL.value,
                branch_id=context.branch_id,
                account_id=context.accounts["112"],
                currency_code="USD",
                exchange_rate=Decimal("25000"),
                debit=Decimal("2500000.00"),
                debit_fc=Decimal("100.00"),
                detail_kind=OpeningDetailKind.ACCOUNT,
            )
        )
        session.flush()

        carry_forward(session, dataset_alpha, context)
        rows = target_rows(session, context, target_year_id, ledger=Ledger.FINANCIAL)
        usd = next(row for row in rows if row.currency_code == "USD")
        assert usd.debit_fc == Decimal("100.00")
        assert usd.debit == Decimal("2500000.00")
        assert usd.exchange_rate == Decimal("25000.000000")
