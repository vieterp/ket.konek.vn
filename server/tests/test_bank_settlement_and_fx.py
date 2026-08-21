"""Đối trừ công nợ + chênh lệch tỷ giá trên chứng từ tiền gửi (lát 6C).

Lưới 8 hướng dấu của FR-SYS-066 đã ghim ở `test_cash_settlement_and_fx.py` —
cơ chế giờ dùng chung (`ket.posting.settlements`), nên ở đây chỉ kiểm phần
BUỘC RIÊNG của module ngân hàng (FR-BNK-006/007):

* UNC đối trừ hóa đơn phải trả USD, tỷ giá thanh toán cao hơn ghi nhận → lỗ
  (Nợ 635), số VND giải phóng trên đích theo tỷ giá ghi nhận; unpost gỡ đúng.
* BC đối trừ hóa đơn phải thu, tỷ giá tăng → lãi (Có 515).
* Chuyển tiền nội bộ không nhận đối trừ (schema chặn — kiểm ở
  `test_bank_voucher_service_flow.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data, seed_open_invoice
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.bank.models import BankSettlement, BankVoucherKind
from ket.modules.bank.schemas import BankSettlementIn, BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.opening_balances.models import OpeningBalanceInvoice, OpeningDetailKind
from posting_support import USD_RATE, PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_20 = date(2026, 1, 20)
VENDOR = 921
CUSTOMER = 922


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    seeded = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return seeded


@pytest.fixture(scope="module")
def usd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code="0021-BANK-USD-FX", currency_code="USD"
    )


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


def _bank_voucher(
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
    *,
    kind: int,
    operation: str,
    partner: tuple[PartnerKind, int],
    fc: Decimal,
    rate: Decimal,
    settlement_target: object,
) -> BankVoucherIn:
    money_in = kind == BankVoucherKind.CREDIT_ADVICE
    partner_kind, partner_id = partner
    business_code = "131" if money_in else "331"
    return BankVoucherIn(
        kind=kind,
        operation_code=operation,
        bank_account_id=bank_account_id,
        branch_id=context.branch_id,
        document_date=JAN_20,
        posting_date=JAN_20,
        currency_code="USD",
        exchange_rate=rate,
        partner_kind=partner_kind,
        partner_id=partner_id,
        lines=(
            BankVoucherLineIn(
                debit_account_id=accounts["112"] if money_in else accounts[business_code],
                credit_account_id=accounts[business_code] if money_in else accounts["112"],
                amount_fc=fc,
                partner_kind=partner_kind,
                partner_id=partner_id,
            ),
        ),
        settlements=(
            BankSettlementIn(
                target_kind=SettlementTargetKind.OPENING_BALANCE,
                target_id=settlement_target,  # type: ignore[arg-type]
                amount_fc=fc,
            ),
        ),
    )


def test_payment_order_settles_payable_with_fx_loss_and_reverts_on_unpost(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    usd_account: int,
) -> None:
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=VENDOR,
        currency_code="USD",
        exchange_rate=USD_RATE,
        amount_fc=Decimal(100),
        invoice_no="HD-BNK-FX-UNC",
    )
    fc = Decimal(10)
    voucher_rate = USD_RATE + Decimal(1000)
    expected_diff = fc * Decimal(1000)

    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(
            _bank_voucher(
                context,
                accounts,
                usd_account,
                kind=BankVoucherKind.PAYMENT_ORDER,
                operation="tra-no-ncc",
                partner=(PartnerKind.VENDOR, VENDOR),
                fc=fc,
                rate=voucher_rate,
                settlement_target=invoice_id,
            ),
            user_id=ACTOR_ID,
        )
        stored = session.execute(
            select(BankSettlement).where(BankSettlement.voucher_id == voucher.id)
        ).scalar_one()
        assert stored.fx_diff == expected_diff

        service.post(voucher.id, user_id=ACTOR_ID)
        rows = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL.value)
            )
            .scalars()
            .all()
        )
        # Chi + tỷ giá tăng → LỖ: Nợ 635 / Có 331 phần chênh.
        loss_rows = [row for row in rows if row.account_id == accounts["635"]]
        assert len(loss_rows) == 1
        assert loss_rows[0].debit == expected_diff and loss_rows[0].credit == 0

        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None
        assert invoice.paid_amount == fc * USD_RATE
        assert invoice.paid_amount_fc == fc

        service.unpost(voucher.id, user_id=ACTOR_ID)
        reverted = session.get(OpeningBalanceInvoice, invoice_id)
        assert reverted is not None
        assert reverted.paid_amount == 0 and reverted.paid_amount_fc == 0
        return None

    run(work)


def test_credit_advice_settles_receivable_with_fx_gain(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    usd_account: int,
) -> None:
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.RECEIVABLE,
        account_code="131",
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER,
        currency_code="USD",
        exchange_rate=USD_RATE,
        amount_fc=Decimal(100),
        invoice_no="HD-BNK-FX-BC",
    )
    fc = Decimal(4)
    voucher_rate = USD_RATE + Decimal(500)
    expected_diff = fc * Decimal(500)

    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(
            _bank_voucher(
                context,
                accounts,
                usd_account,
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation="thu-no-khach-hang",
                partner=(PartnerKind.CUSTOMER, CUSTOMER),
                fc=fc,
                rate=voucher_rate,
                settlement_target=invoice_id,
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        rows = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL.value)
            )
            .scalars()
            .all()
        )
        # Thu + tỷ giá tăng → LÃI: Nợ 131 / Có 515 phần chênh.
        gain_rows = [row for row in rows if row.account_id == accounts["515"]]
        assert len(gain_rows) == 1
        assert gain_rows[0].credit == expected_diff and gain_rows[0].debit == 0
        return None

    run(work)
