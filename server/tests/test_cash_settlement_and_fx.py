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

from cash_book_support import (
    seed_cash_book_package_data,
    seed_open_invoice,
    seed_opening_advance,
)
from ket.kernel.config.accounts_models import ChartOfAccount
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
from ket.posting.integrity.checks.registry import check_of
from ket.posting.integrity.runner import run_check
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
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loại đích chưa có chủ bị từ chối rõ ràng.

    Viết ở 6B khi hóa đơn bán **thật sự** chưa có source. Lát 7A đăng ký source
    cho cả `SALES_INVOICE` lẫn `PURCHASE_INVOICE`, nên không còn giá trị enum
    nào thiếu chủ để mượn — nhưng bất biến thì vẫn sống: một bản cài KHÔNG có
    module `receivables` phải từ chối bằng `settlement.kind_unavailable` chứ
    không nuốt dòng đối trừ.

    Nên thay vì xóa bài, mô phỏng sự vắng mặt ngay tại cái khe mà
    `posting/settlements.py` hỏi — `PROVIDERS.settlement_source` — chứ không
    chọc vào dict private của registry.
    """
    # `delitem` trên chính sổ đăng ký, không `setattr` lên singleton: vá
    # phương thức của một instance dùng chung thì lượt undo của pytest ghi bound
    # method thành THUỘC TÍNH INSTANCE vĩnh viễn — hành vi y hệt, nhưng registry
    # không còn nguyên vẹn cho các tệp chạy sau. `delitem` khôi phục đúng mục đã
    # gỡ, và nó mô phỏng đúng thứ đang mô phỏng: bản cài vắng mặt.
    monkeypatch.delitem(
        PROVIDERS._settlement_sources,
        SettlementTargetKind.SALES_INVOICE,
    )

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


def _mixed_receipt(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    debt_amount_fc: Decimal,
    other_amount_fc: Decimal,
    settlements: tuple[CashSettlementIn, ...],
) -> CashVoucherIn:
    """Phiếu thu gộp: một dòng thu nợ khách + một dòng không chạm công nợ."""
    return CashVoucherIn(
        kind=CashVoucherKind.RECEIPT,
        operation_code="thu-no-khach-hang",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_20,
        posting_date=JAN_20,
        currency_code="VND",
        exchange_rate=Decimal(1),
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER,
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["131"],
                amount_fc=debt_amount_fc,
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=CUSTOMER,
            ),
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["3381"],
                amount_fc=other_amount_fc,
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=CUSTOMER,
            ),
        ),
        settlements=settlements,
    )


def test_settlement_total_is_measured_on_debt_lines_not_on_the_voucher(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Lát 7C-5, điều kiện #9: BR-QUY-03 đo trên DÒNG CÔNG NỢ.

    Phiếu thu 150 gồm `Có 131 khách A 100` + `Có 3381 50` đối trừ 100 vào hóa
    đơn của A là chứng từ ĐÚNG — sổ cái và sổ phụ cùng nhích 100. Bản trước lát
    này bắt đối trừ đủ 150 (tổng MỌI dòng), tức bắt người dùng khai một con số
    mà sổ cái không hề ghi.
    """
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("500000"),
        invoice_no="HD-DEBTLINE",
    )

    def work(session: Session) -> object:
        created = CashVoucherService(session).create(
            _mixed_receipt(
                context,
                accounts,
                debt_amount_fc=Decimal("100000"),
                other_amount_fc=Decimal("50000"),
                settlements=(_settle(invoice_id, Decimal("100000")),),
            ),
            user_id=ACTOR_ID,
        )
        assert created is not None

        # Kiểm chứng ngược: đối trừ đủ tổng chứng từ (150) nay là VI PHẠM, vì
        # sổ cái chỉ ghi giảm 100 trên TK công nợ.
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(
                _mixed_receipt(
                    context,
                    accounts,
                    debt_amount_fc=Decimal("100000"),
                    other_amount_fc=Decimal("50000"),
                    settlements=(_settle(invoice_id, Decimal("150000")),),
                ),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "settlement.total_mismatch"
        return None

    run(work)


def test_debt_line_of_another_partner_is_refused_when_the_voucher_settles(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Khối đối trừ nhận đối tác ở HEADER còn sổ cái ghi theo đối tác từng dòng
    — hai chỗ lệch nhau thì không vế nào của đẳng thức đo được cái kia."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("500000"),
        invoice_no="HD-OTHERPARTNER",
    )

    def work(session: Session) -> object:
        payload = _voucher(
            context,
            accounts,
            kind=CashVoucherKind.RECEIPT,
            amount_fc=Decimal("100000"),
            settlements=(_settle(invoice_id, Decimal("100000")),),
        )
        stray = payload.model_copy(
            update={"lines": (payload.lines[0].model_copy(update={"partner_id": CUSTOMER + 1}),)}
        )
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(stray, user_id=ACTOR_ID)
        assert [violation.code for violation in caught.value.violations] == [
            "settlement.line_partner_mismatch"
        ]
        return None

    run(work)


def test_debt_lines_spread_over_two_accounts_are_refused_when_the_voucher_settles(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Đích đối trừ chỉ nhận MỘT `account_id` (mua/bán/GLE truyền từ 7B), nên
    hai TK công nợ trên một chứng từ tiền là hai phạm vi không đo chung được."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("500000"),
        invoice_no="HD-TWOACCOUNTS",
    )

    def work(session: Session) -> object:
        second = session.get(ChartOfAccount, accounts["1381"])
        assert second is not None
        second.detail_tracking = ["customer"]
        session.flush()
        try:
            payload = _voucher(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                amount_fc=Decimal("100000"),
                settlements=(_settle(invoice_id, Decimal("150000")),),
            )
            spread = payload.model_copy(
                update={
                    "lines": (
                        *payload.lines,
                        CashVoucherLineIn(
                            debit_account_id=accounts["111"],
                            credit_account_id=accounts["1381"],
                            amount_fc=Decimal("50000"),
                            partner_kind=PartnerKind.CUSTOMER,
                            partner_id=CUSTOMER,
                        ),
                    )
                }
            )
            with pytest.raises(PostingValidationError) as caught:
                CashVoucherService(session).create(spread, user_id=ACTOR_ID)
            assert [violation.code for violation in caught.value.violations] == [
                "settlement.line_account_spread"
            ]
        finally:
            second.detail_tracking = None
            session.flush()
        return None

    run(work)


def test_opening_advance_is_listed_payable_and_settled_by_a_payment(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Điều kiện #7: khoản khách ứng trước ở SỐ DƯ ĐẦU KỲ đối trừ được.

    Nó nằm trên dòng cha nhóm PHẢI THU nhưng mang chiều PHẢI TRẢ — tiền ta đang
    giữ của khách — nên nó phải hiện ở view phải trả và tất toán bằng phiếu chi.
    """
    advance_id = seed_opening_advance(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount=Decimal("300000"),
    )

    def work(session: Session) -> object:
        payable = open_invoices(
            session,
            side="payable",
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=CUSTOMER,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        assert [invoice.target_id for invoice in payable] == [advance_id]
        assert payable[0].target_kind is SettlementTargetKind.OPENING_ADVANCE
        # Chiều ngược lại KHÔNG được thấy nó: liệt kê khoản ứng trước ở màn thu
        # tiền là mời người dùng thu thêm một lần số tiền họ đang nợ lại khách.
        # Đo bằng phép vắng mặt chứ không bằng danh sách rỗng — các bài khác
        # của tệp đã gieo hóa đơn cho chính khách này vào dataset dùng chung.
        receivable = open_invoices(
            session,
            side="receivable",
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=CUSTOMER,
            branch_id=context.branch_id,
            as_of=JAN_20,
        )
        assert advance_id not in {invoice.target_id for invoice in receivable}
        assert all(
            invoice.target_kind is not SettlementTargetKind.OPENING_ADVANCE
            for invoice in receivable
        )

        payment = _voucher(
            context,
            accounts,
            kind=CashVoucherKind.PAYMENT,
            amount_fc=Decimal("300000"),
            settlements=(
                CashSettlementIn(
                    target_kind=SettlementTargetKind.OPENING_ADVANCE,
                    target_id=advance_id,
                    amount_fc=Decimal("300000"),
                ),
            ),
        )
        # Phiếu CHI trả lại tiền cho KHÁCH: nghiệp vụ "chi khác" (nghiệp vụ trả
        # nợ NCC khóa loại đối tác là nhà cung cấp), dòng ghi Nợ 131 chứ không
        # phải 331.
        refund = payment.model_copy(
            update={
                "operation_code": "chi-khac",
                "lines": (
                    CashVoucherLineIn(
                        debit_account_id=accounts["131"],
                        credit_account_id=accounts["111"],
                        amount_fc=Decimal("300000"),
                        partner_kind=PartnerKind.CUSTOMER,
                        partner_id=CUSTOMER,
                    ),
                ),
            }
        )
        service = CashVoucherService(session)
        created = service.create(refund, user_id=ACTOR_ID)
        assert created is not None
        settled = session.get(OpeningBalanceInvoice, advance_id)
        assert settled is not None
        assert settled.paid_amount_fc == Decimal(0)  # cộng lúc GHI SỔ, không lúc lập

        # GHI SỔ thật, không dừng ở `create`: hai check toàn vẹn chỉ nhìn chứng
        # từ `status = 2`, nên một bài dừng ở bản nháp không chứng minh được
        # điều gì về chúng — và chính lỗ ấy để lọt việc
        # `settlement_matches_subledger` đóng cứng loại đích 2 cho cả bảng chi
        # tiết đầu kỳ (dòng ứng trước nộp loại 7).
        service.post(created.id, user_id=ACTOR_ID)
        session.flush()
        session.refresh(settled)
        assert settled.paid_amount_fc == Decimal("300000.00")
        assert settled.paid_amount == Decimal("300000.00")

        for code in ("settlement_matches_subledger", "arap_matches_control"):
            outcome = run_check(session, check_of(code), branch_id=context.branch_id)
            assert not [
                row
                for row in outcome.sample
                if row.get("target_id") == str(advance_id) or row.get("partner_id") == CUSTOMER
            ], (code, outcome.sample)
        return None

    run(work)


def test_many_partners_on_one_voucher_stay_valid_without_a_settlement_block(
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Phạm vi #9 chỉ áp khi chứng từ CÓ khối đối trừ.

    Phiếu thu gộp tiền của hai khách trong một lượt, không đối trừ hóa đơn nào,
    là chứng từ hợp lệ: mỗi dòng sinh một khoản sổ phụ riêng, và không có con
    số ở mức chứng từ nào để hai vế phải khớp. Siết nó là cấm một hình dạng mà
    lát này không có lý do đụng tới — bản đầu của lát đã cấm nhầm đúng ca này.
    """

    def work(session: Session) -> object:
        payload = CashVoucherIn(
            kind=CashVoucherKind.RECEIPT,
            operation_code="thu-khac",
            cash_account_id=accounts["111"],
            branch_id=context.branch_id,
            document_date=JAN_20,
            posting_date=JAN_20,
            currency_code="VND",
            exchange_rate=Decimal(1),
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=CUSTOMER,
            lines=(
                CashVoucherLineIn(
                    debit_account_id=accounts["111"],
                    credit_account_id=accounts["131"],
                    amount_fc=Decimal("120000"),
                    partner_kind=PartnerKind.CUSTOMER,
                    partner_id=CUSTOMER,
                ),
                CashVoucherLineIn(
                    debit_account_id=accounts["111"],
                    credit_account_id=accounts["131"],
                    amount_fc=Decimal("80000"),
                    partner_kind=PartnerKind.CUSTOMER,
                    partner_id=CUSTOMER + 2,
                ),
            ),
            settlements=(),
        )
        created = CashVoucherService(session).create(payload, user_id=ACTOR_ID)
        assert created is not None
        return None

    run(work)


def test_debt_line_against_the_settlement_direction_is_refused(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Tổng dòng công nợ chỉ có nghĩa khi mọi dòng cùng CHIỀU với lượt đối trừ.

    `settles_advance` là một giá trị cho cả chứng từ, còn dòng thì có chiều
    riêng — và dòng không chạm quỹ được phép ở phiếu thu/chi. Phiếu thu gồm
    `Có 131 khách A 100.000` **và** `Nợ 131 khách A 20.000` vì thế có tổng dòng
    công nợ 120.000 trong khi sổ cái chỉ nhích 80.000: cộng không dấu thì đối
    trừ 120.000 lọt cổng và `arap_matches_control` lệch 40.000.
    """
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("500000"),
        invoice_no="HD-DIRECTION",
    )

    def work(session: Session) -> object:
        payload = _voucher(
            context,
            accounts,
            kind=CashVoucherKind.RECEIPT,
            amount_fc=Decimal("100000"),
            settlements=(_settle(invoice_id, Decimal("120000")),),
        )
        both_ways = payload.model_copy(
            update={
                "lines": (
                    *payload.lines,
                    CashVoucherLineIn(
                        debit_account_id=accounts["131"],
                        credit_account_id=accounts["3381"],
                        amount_fc=Decimal("20000"),
                        partner_kind=PartnerKind.CUSTOMER,
                        partner_id=CUSTOMER,
                    ),
                )
            }
        )
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(both_ways, user_id=ACTOR_ID)
        assert [violation.code for violation in caught.value.violations] == [
            "settlement.line_direction_mismatch"
        ]
        return None

    run(work)
