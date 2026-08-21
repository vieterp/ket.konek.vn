"""Đối trừ công nợ + chênh lệch tỷ giá thu/trả tiền (SRS 03 §4, FR-SYS-066) — lát 6B.

Nguồn đích duy nhất của v1 là số dư ban đầu (`opening_balance_invoices`), cài
`ReceivableProvider`/`PayableProvider`/`SettlementTargetSource` ở
`posting/opening_balances/settlement_source.py`. Bảng kịch bản tỷ giá theo
đúng ô rủi ro của phase file: **tăng/giảm × thu/chi** — 4 hướng dấu, mỗi hướng
kiểm cả dòng 515/635 lẫn số VND giải phóng trên TK công nợ.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data, seed_open_invoice
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import PROVIDERS, SettlementTargetKind
from ket.modules.cash_book.models import CashSettlement, CashVoucherKind
from ket.modules.cash_book.schemas import CashSettlementIn, CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.cash_book.settlement_service import open_invoices
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.opening_balances.models import OpeningBalanceInvoice, OpeningDetailKind
from posting_support import USD_RATE, PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_20 = date(2026, 1, 20)
CUSTOMER = 8101
VENDOR = 8202


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


def _voucher(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    kind: int,
    amount_fc: Decimal,
    settlements: tuple[CashSettlementIn, ...],
    currency: str = "VND",
    rate: Decimal = Decimal(1),
    partner: tuple[PartnerKind, int] = (PartnerKind.CUSTOMER, CUSTOMER),
) -> CashVoucherIn:
    receivable = kind == CashVoucherKind.RECEIPT
    partner_kind, partner_id = partner
    business_account = accounts["131"] if receivable else accounts["331"]
    return CashVoucherIn(
        kind=kind,
        operation_code="thu-no-khach-hang" if receivable else "tra-no-ncc",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_20,
        posting_date=JAN_20,
        currency_code=currency,
        exchange_rate=rate,
        partner_kind=partner_kind,
        partner_id=partner_id,
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"] if receivable else business_account,
                credit_account_id=business_account if receivable else accounts["111"],
                amount_fc=amount_fc,
                partner_kind=partner_kind,
                partner_id=partner_id,
            ),
        ),
        settlements=settlements,
    )


def _settle(invoice_id: object, amount_fc: Decimal) -> CashSettlementIn:
    return CashSettlementIn(
        target_kind=SettlementTargetKind.OPENING_BALANCE,
        target_id=invoice_id,  # type: ignore[arg-type]
        amount_fc=amount_fc,
    )


def test_open_invoices_list_remaining_and_shrink_after_posting(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("900000"),
        invoice_no="HD-OPEN-1",
    )

    def work(session: Session) -> object:
        listed = open_invoices(
            session,
            side="receivable",
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=CUSTOMER,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        target = next(item for item in listed if item.target_id == invoice_id)
        assert target.remaining_fc == Decimal("900000")
        assert target.account_id == context.accounts["131"]

        service = CashVoucherService(session)
        voucher = service.create(
            _voucher(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                amount_fc=Decimal("400000"),
                settlements=(_settle(invoice_id, Decimal("400000")),),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None
        assert invoice.paid_amount_fc == Decimal("400000")
        assert invoice.paid_amount == Decimal("400000")

        relisted = open_invoices(
            session,
            side="receivable",
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=CUSTOMER,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        target_after = next(item for item in relisted if item.target_id == invoice_id)
        assert target_after.remaining_fc == Decimal("500000")

        # Bỏ ghi sổ gỡ đúng số đã cộng.
        service.unpost(voucher.id, user_id=ACTOR_ID)
        invoice_after = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice_after is not None and invoice_after.paid_amount_fc == Decimal(0)
        return None

    run(work)


def test_settlement_validation_reports_all_mismatches(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("100000"),
        invoice_no="HD-OPEN-2",
    )

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        # Vượt số còn nợ (BR-QUY-02) và lệch tổng phiếu (BR-QUY-03) cùng lộ.
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal("100000"),
                    settlements=(_settle(invoice_id, Decimal("150000")),),
                ),
                user_id=ACTOR_ID,
            )
        codes = {violation.code for violation in caught.value.violations}
        assert "settlement.exceeds_remaining" in codes

        # Đối tác khác trên phiếu → partner_mismatch.
        with pytest.raises(PostingValidationError) as partner_caught:
            service.create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal("50000"),
                    settlements=(_settle(invoice_id, Decimal("50000")),),
                    partner=(PartnerKind.CUSTOMER, CUSTOMER + 1),
                ),
                user_id=ACTOR_ID,
            )
        partner_codes = {violation.code for violation in partner_caught.value.violations}
        assert "settlement.partner_mismatch" in partner_codes

        # Đích không tồn tại.
        with pytest.raises(PostingValidationError) as missing_caught:
            service.create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal("50000"),
                    settlements=(
                        _settle("00000000-0000-0000-0000-0000000000aa", Decimal("50000")),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        missing_codes = {violation.code for violation in missing_caught.value.violations}
        assert "settlement.target_missing" in missing_codes
        return None

    run(work)


def test_two_vouchers_cannot_oversettle_the_same_invoice(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """BR-QUY-02 lần cuối ở `apply` dưới `FOR UPDATE`: hai phiếu hợp lệ lúc
    cất, tổng vượt số còn nợ — phiếu ghi sổ sau phải bị từ chối."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("100000"),
        invoice_no="HD-OPEN-3",
    )

    voucher_ids: list[object] = []

    def create_and_post_first(session: Session) -> object:
        service = CashVoucherService(session)
        first = service.create(
            _voucher(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                amount_fc=Decimal("80000"),
                settlements=(_settle(invoice_id, Decimal("80000")),),
            ),
            user_id=ACTOR_ID,
        )
        second = service.create(
            _voucher(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                amount_fc=Decimal("70000"),
                settlements=(_settle(invoice_id, Decimal("70000")),),
            ),
            user_id=ACTOR_ID,
        )
        service.post(first.id, user_id=ACTOR_ID)
        voucher_ids.append(second.id)
        return None

    run(create_and_post_first)

    # Ghi sổ phiếu thứ hai ở transaction RIÊNG và để ngoại lệ lan ra ngoài —
    # trong sản xuất `unit_of_work` rollback trọn lượt, giữ nguyên bất biến
    # "không có chứng từ ghi-sổ-nửa-chừng".
    def post_second(session: Session) -> object:
        CashVoucherService(session).post(voucher_ids[0], user_id=ACTOR_ID)  # type: ignore[arg-type]
        return None

    with pytest.raises(PostingValidationError) as caught:
        run(post_second)
    assert caught.value.violations[0].code == "settlement.exceeds_remaining"


@pytest.mark.parametrize(
    ("kind", "voucher_rate", "expected_fx_code", "expected_side_debit"),
    [
        # (loại phiếu, tỷ giá phiếu, TK nhận phần chênh, TK công nợ đứng bên Nợ?)
        (CashVoucherKind.RECEIPT, Decimal(26_000), "515", True),
        (CashVoucherKind.RECEIPT, Decimal(24_000), "635", False),
        (CashVoucherKind.PAYMENT, Decimal(26_000), "635", False),
        (CashVoucherKind.PAYMENT, Decimal(24_000), "515", True),
    ],
)
def test_fx_difference_grid_receipt_and_payment(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
    kind: int,
    voucher_rate: Decimal,
    expected_fx_code: str,
    expected_side_debit: bool,
) -> None:
    """Bốn hướng của FR-SYS-066 trên hóa đơn USD ghi nhận tỷ giá 25.000:

    * thu + tỷ giá tăng → lãi (Có 515), TK công nợ được Nợ thêm phần chênh;
    * thu + tỷ giá giảm → lỗ (Nợ 635);
    * chi + tỷ giá tăng → lỗ (Nợ 635);
    * chi + tỷ giá giảm → lãi (Có 515).

    Số VND giải phóng trên đích luôn là `fc × 25.000` — không phụ thuộc tỷ giá
    lúc thanh toán.
    """
    receivable = kind == CashVoucherKind.RECEIPT
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=(OpeningDetailKind.RECEIVABLE if receivable else OpeningDetailKind.PAYABLE),
        account_code="131" if receivable else "331",
        partner_kind=PartnerKind.CUSTOMER if receivable else PartnerKind.VENDOR,
        partner_id=CUSTOMER if receivable else VENDOR,
        currency_code="USD",
        exchange_rate=USD_RATE,
        amount_fc=Decimal(100),
        invoice_no=f"HD-FX-{kind}-{voucher_rate}",
    )
    fc = Decimal(10)
    expected_diff = abs(fc * voucher_rate - fc * USD_RATE)

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(
            _voucher(
                context,
                accounts,
                kind=kind,
                amount_fc=fc,
                settlements=(_settle(invoice_id, fc),),
                currency="USD",
                rate=voucher_rate,
                partner=(
                    (PartnerKind.CUSTOMER, CUSTOMER) if receivable else (PartnerKind.VENDOR, VENDOR)
                ),
            ),
            user_id=ACTOR_ID,
        )
        stored = session.execute(
            select(CashSettlement).where(CashSettlement.voucher_id == voucher.id)
        ).scalar_one()
        assert abs(stored.fx_diff) == expected_diff
        service.post(voucher.id, user_id=ACTOR_ID)

        rows = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL.value)
                .order_by(GlPosting.line_no)
            )
            .scalars()
            .all()
        )
        fx_account_id = accounts[expected_fx_code]
        fx_rows = [row for row in rows if row.account_id == fx_account_id]
        assert len(fx_rows) == 1
        fx_row = fx_rows[0]
        if expected_side_debit:
            # TK công nợ bên Nợ → TK 515 nhận bên Có (lãi).
            assert fx_row.credit == expected_diff and fx_row.debit == 0
        else:
            assert fx_row.debit == expected_diff and fx_row.credit == 0

        business_account = context.accounts["131" if receivable else "331"]
        adjustment_rows = [
            row for row in rows if row.account_id == business_account and row.currency_code == "VND"
        ]
        assert len(adjustment_rows) == 1
        adjustment = adjustment_rows[0]
        assert (adjustment.debit if expected_side_debit else adjustment.credit) == expected_diff
        assert adjustment.partner_id == (CUSTOMER if receivable else VENDOR)

        # Số VND giải phóng trên đích = fc × tỷ giá ghi nhận, mọi kịch bản.
        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None
        assert invoice.paid_amount == fc * USD_RATE
        assert invoice.paid_amount_fc == fc

        service.unpost(voucher.id, user_id=ACTOR_ID)
        reverted = session.get(OpeningBalanceInvoice, invoice_id)
        assert reverted is not None and reverted.paid_amount == 0
        return None

    run(work)


def test_settlement_kind_without_a_source_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Loại đích chưa có chủ (hóa đơn bán — phase 7) bị từ chối rõ ràng."""
    assert PROVIDERS.settlement_source(SettlementTargetKind.SALES_INVOICE) is None

    def work(session: Session) -> object:
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal("10000"),
                    settlements=(
                        CashSettlementIn(
                            target_kind=SettlementTargetKind.SALES_INVOICE,
                            target_id="00000000-0000-0000-0000-0000000000bb",  # type: ignore[arg-type]
                            amount_fc=Decimal("10000"),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        codes = {violation.code for violation in caught.value.violations}
        assert "settlement.kind_unavailable" in codes
        return None

    run(work)


def test_partial_settlements_with_odd_rate_never_overflow_the_vnd_check(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Sửa H-2 review 6B: hóa đơn 3 USD @25.000,005 (amount = 75.000,02 VND);
    ba lát ×1 USD làm tròn riêng cho 3 × 25.000,01 = 75.000,03 — trước sửa,
    lát cuối nổ CHECK `paid_within_amount` thành IntegrityError 500. Nay lát
    CUỐI gánh phần lẻ: giải phóng đúng số VND còn treo, phần dư 0,01 đi vào
    515/635 như chênh lệch tỷ giá."""
    odd_rate = Decimal("25000.005")
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        currency_code="USD",
        exchange_rate=odd_rate,
        amount_fc=Decimal(3),
        invoice_no="HD-ODD-RATE",
    )

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        for _ in range(3):
            voucher = service.create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal(1),
                    settlements=(_settle(invoice_id, Decimal(1)),),
                    currency="USD",
                    rate=odd_rate,
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)

        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None
        assert invoice.paid_amount_fc == Decimal(3)
        # VND giải phóng đúng bằng giá trị ghi nhận — không tràn, không treo lẻ.
        assert invoice.paid_amount == invoice.amount
        return None

    run(work)


def test_final_settlement_drains_the_vnd_leftover_when_rate_rounds_down(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Hướng NGƯỢC của ca odd-rate (review 6C, LOW-2 — mutation thay luật
    lát-cuối bằng `min()` thuần từng sống): hóa đơn 3 USD @25.000,004 (amount =
    75.000,01 VND); ba lát ×1 USD làm tròn riêng chỉ 3 × 25.000,00 = 75.000,00
    — `min()` thuần để 0,01 VND treo mãi (`remaining > 0` khi
    `remaining_fc = 0`, không ai đối trừ được nữa). Lát CUỐI phải vét trọn số
    VND còn treo."""
    odd_rate = Decimal("25000.004")
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        currency_code="USD",
        exchange_rate=odd_rate,
        amount_fc=Decimal(3),
        invoice_no="HD-ODD-RATE-DOWN",
    )

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        for _ in range(3):
            voucher = service.create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal(1),
                    settlements=(_settle(invoice_id, Decimal(1)),),
                    currency="USD",
                    rate=odd_rate,
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)

        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None
        assert invoice.paid_amount_fc == Decimal(3)
        # Vét trọn: không còn một xu VND treo sau khi nguyên tệ về 0.
        assert invoice.paid_amount == invoice.amount
        return None

    run(work)


def test_apply_refuses_a_vnd_overflow_as_a_domain_error(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    run: Runner,
) -> None:
    """Lớp chặn cuối của H-2: `apply` bị đưa số VND vượt số còn treo (đường
    đua/pricing lỗi) phải từ chối bằng vi phạm nghiệp vụ 422, không để CHECK
    của DB nổ thành IntegrityError."""
    from ket.posting.opening_balances.settlement_source import SOURCE

    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("100000"),
        invoice_no="HD-VND-GUARD",
    )

    def work(session: Session) -> object:
        with pytest.raises(PostingValidationError) as caught:
            SOURCE.apply(
                session,
                target_id=invoice_id,
                amount_fc=Decimal("50000"),
                amount=Decimal("100001"),
            )
        assert caught.value.violations[0].code == "settlement.exceeds_remaining"
        return None

    run(work)


def test_settlement_total_must_equal_voucher_total(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """BR-QUY-03 với mọi đích HỢP LỆ (đóng lỗ test L-2 review 6B — mutation
    M12 bỏ phép so tổng từng sống sót)."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("500000"),
        invoice_no="HD-BRQUY03",
    )

    def work(session: Session) -> object:
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(
                _voucher(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    amount_fc=Decimal("300000"),
                    settlements=(_settle(invoice_id, Decimal("200000")),),
                ),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "settlement.total_mismatch"
        return None

    run(work)


def test_receivable_registry_serves_no_payables_and_vice_versa(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    run: Runner,
) -> None:
    """Sửa H-1 review 6B ở tầng dữ liệu: provider đăng ký cho chiều PHẢI THU
    khóa cứng vào nhóm phải thu — hỏi nó về NCC (phải trả) ra rỗng, dù cùng
    một nguồn số dư ban đầu cài cả hai chiều."""
    lone_vendor = VENDOR + 71
    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=lone_vendor,
        amount_fc=Decimal("70000"),
        invoice_no="HD-AP-SIDE",
    )

    def work(session: Session) -> object:
        via_receivable = open_invoices(
            session,
            side="receivable",
            partner_kind=PartnerKind.VENDOR,
            partner_id=lone_vendor,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        assert via_receivable == ()
        via_payable = open_invoices(
            session,
            side="payable",
            partner_kind=PartnerKind.VENDOR,
            partner_id=lone_vendor,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        assert [item.invoice_no for item in via_payable] == ["HD-AP-SIDE"]
        return None

    run(work)


def test_settlement_target_lock_is_for_update() -> None:
    """Ghim `FOR UPDATE` trong câu khóa đích đối trừ (mutation M4 review 6B —
    bỏ khóa đi thì mọi test một-luồng vẫn xanh; cùng lối ghim SQL biên dịch
    của `period_share_lock_statement` phase 4)."""
    from sqlalchemy.dialects import postgresql

    from ket.posting.opening_balances.models import OpeningBalanceInvoice as Invoice

    statement = (
        select(Invoice).where(Invoice.id == None).with_for_update()  # noqa: E711
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled

    # Câu thật trong `_lock` phải giữ đúng cờ đó — đọc thẳng mã nguồn để một
    # lần xóa `.with_for_update()` không thể sống qua test này.
    import inspect

    from ket.posting.opening_balances import settlement_source

    source = inspect.getsource(settlement_source.OpeningBalanceSettlementSource._lock)
    assert "with_for_update" in source
