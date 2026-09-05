"""Vòng đời hóa đơn bán hàng trên PostgreSQL thật (lát 7C-2).

Kiểm những bất biến RIÊNG của phân hệ bán — luật ghi sổ chung đã có
`test_posting_engine_flow.py`, sổ phụ công nợ có `test_ar_ap_ledger.py`, và
phần đối xứng với chiều mua đã ghim ở `test_purchase_invoice_flow.py`:

* Cất cấp số `SAL{YY}-`; nghiệp vụ phải thuộc gói (FR-SYS-025) và phải là
  nghiệp vụ của khách hàng; TK công nợ phải theo dõi khách hàng.
* Chiết khấu thương mại trừ ngay trên dòng: doanh thu ghi số **sau** chiết
  khấu, `total_discount_fc` giữ phần đã giảm, và không có bút toán 521 nào.
* Mapper: Nợ TK phải thu mang chiều khách hàng, Có TK doanh thu và TK thuế
  đầu ra mang chiều vật tư; không có cặp giá vốn nào (phase 8).
* Ghi sổ → sổ phụ có đúng **một** khoản phải thu; bỏ ghi sổ → gỡ sạch; số VND
  của sổ phụ khớp sổ cái từng đồng kể cả ngoại tệ.
* Trả lại hàng bán và giảm giá hàng bán đối trừ hóa đơn gốc: bút toán đảo
  chiều, `settled` của khoản gốc tăng rồi giảm theo ghi sổ / bỏ ghi sổ.
* **Hóa đơn gốc đã thu đủ thì chứng từ giảm trừ không lập được** — quyết định
  user 2026-09-04; đường đúng lúc ấy là trả tiền lại khách bằng phiếu chi.
* Hạn thanh toán rơi về điều khoản khai trên **danh mục khách hàng**
  (FR-SAL-009) — vế mà chiều mua không có.
* Bộ đếm tham chiếu khách hàng và nhân viên bán hàng nhích/lùi theo
  tạo–sửa–xóa (BR-SYS-02).
* **Hai check toàn vẹn có nhánh của phân hệ này**: dữ liệu bán ĐÚNG không
  được làm chúng đỏ. Cả hai check cộng từ một `UNION` các bảng, nên một
  phân hệ mới quên nộp nhánh của mình là một dòng đỏ **trên dữ liệu đúng**
  — và không cổng nào khác nhìn thấy điều đó.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.money import convert_currency
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.receivables.ledger_service import SUBLEDGER_SETTLED_CODE
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.receivables.settlement_source import SETTLEMENT_OVERPAID_CODE
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import (
    KIND_IMMUTABLE_CODE,
    OPERATION_PARTNER_REQUIRED_CODE,
    OPERATION_UNKNOWN_CODE,
    RECEIVABLE_ACCOUNT_NOT_CUSTOMER_TRACKED_CODE,
    SalesInvoiceService,
)
from ket.posting.contracts import Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.integrity.checks.registry import check_of
from ket.posting.integrity.runner import run_check
from ket.posting.settlements import SETTLEMENT_ACCOUNT_MISMATCH_CODE
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_payment_term
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)
JAN_20 = date(2026, 1, 20)

CUSTOMER_ID = 9201
TERM_CUSTOMER_ID = 9202
SALESPERSON_ID = 9203


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    accounts = seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7C2-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7C2-01")
    return accounts


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


def _sales_invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    operation: str = "ban-hang-hoa",
    kind: int = SalesInvoiceKind.GOODS,
    settlements: tuple[SalesSettlementIn, ...] = (),
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    posting_date: date = JAN_15,
    customer_id: int = CUSTOMER_ID,
    lines: tuple[SalesInvoiceLineIn, ...] | None = None,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        operation_code=operation,
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000456",
        description="bán hàng test",
        lines=lines
        or (
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(10),
                unit_price_fc=Decimal(100_000),
                # 1 000 000 chiết khấu 5% còn 950 000 — doanh thu ghi số sau
                # chiết khấu, phần đã giảm giữ ở `discount_amount_fc`.
                discount_percent=Decimal(5),
                discount_amount_fc=Decimal(50_000),
                amount_fc=Decimal(950_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(95_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_list_id=None,
                price_source=PriceSource.ITEM_DEFAULT,
            ),
            SalesInvoiceLineIn(
                description="Hàng B",
                quantity=Decimal(5),
                unit_price_fc=Decimal(60_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
            ),
        ),
        settlements=settlements,
    )


def _subledger_rows(session: Session, voucher_id: object) -> list[ArApLedgerEntry]:
    return list(
        session.execute(
            select(ArApLedgerEntry)
            .where(ArApLedgerEntry.document_id == voucher_id)
            .order_by(ArApLedgerEntry.partner_id)
        )
        .scalars()
        .all()
    )


def _postings(
    session: Session, voucher_id: object, ledger: int = Ledger.FINANCIAL
) -> list[GlPosting]:
    return list(
        session.execute(
            select(GlPosting)
            .where(GlPosting.voucher_id == voucher_id, GlPosting.ledger == ledger)
            .order_by(GlPosting.line_no)
        )
        .scalars()
        .all()
    )


def _post_original(
    session: Session, context: PostingContext, accounts: dict[str, int]
) -> tuple[SalesInvoiceService, Voucher, ArApLedgerEntry]:
    """Một hóa đơn bán đã ghi sổ + khoản phải thu của nó."""
    service = SalesInvoiceService(session)
    original = service.create(_sales_invoice(context, accounts), user_id=ACTOR_ID)
    service.post(original.id, user_id=ACTOR_ID)
    (debt,) = _subledger_rows(session, original.id)
    return service, original, debt


def test_create_numbers_keeps_discount_and_counts_catalog_use(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        voucher = service.create(_sales_invoice(context, accounts), user_id=ACTOR_ID)
        assert voucher.voucher_no.startswith("SAL26-")
        assert voucher.document_type == "SAL"

        _, body, lines, settlements = service.get(voucher.id)
        # Doanh thu là số SAU chiết khấu; phần đã giảm đứng riêng.
        assert body.total_before_tax_fc == Decimal(1_250_000)
        assert body.total_discount_fc == Decimal(50_000)
        assert body.total_vat_fc == Decimal(125_000)
        assert body.total_fc == Decimal(1_375_000)
        assert body.cogs_posted is False
        assert settlements == []
        # Nguồn giá lưu lại nguyên như client chốt — không tham gia phép tính nào.
        assert lines[0].price_source == PriceSource.ITEM_DEFAULT.value
        assert lines[1].price_source is None
        assert usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=CUSTOMER_ID) == 1
        assert (
            usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=SALESPERSON_ID) == 1
        )
        return None

    run(work)


def test_operation_must_exist_and_belong_to_customers(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        with pytest.raises(PostingValidationError) as unknown:
            service.create(
                _sales_invoice(context, accounts, operation="khong-co"), user_id=ACTOR_ID
            )
        assert unknown.value.violations[0].code == OPERATION_UNKNOWN_CODE

        with pytest.raises(PostingValidationError) as wrong_kind:
            service.create(
                _sales_invoice(context, accounts, operation="ban-cho-ncc"), user_id=ACTOR_ID
            )
        assert wrong_kind.value.violations[0].code == OPERATION_PARTNER_REQUIRED_CODE
        return None

    run(work)


def test_customer_debt_must_sit_on_a_customer_tracked_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn bán ghi Nợ 111 sinh một dòng sổ phụ mà sổ cái không có, và màn
    đối trừ liệt kê nó mãi — từ chối từ lúc cất."""

    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        payload = _sales_invoice(context, accounts).model_copy(
            update={"receivable_account_id": accounts["111"]}
        )
        with pytest.raises(PostingValidationError) as caught:
            service.create(payload, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == RECEIVABLE_ACCOUNT_NOT_CUSTOMER_TRACKED_CODE
        assert violation.details["account_id"] == accounts["111"]
        return None

    run(work)


def test_post_writes_ledger_and_subledger_then_unpost_clears(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        voucher = service.create(_sales_invoice(context, accounts), user_id=ACTOR_ID)
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO

        postings = _postings(session, voucher.id)
        by_account: dict[int, tuple[Decimal, Decimal]] = {}
        for row in postings:
            debit, credit = by_account.get(row.account_id, (Decimal(0), Decimal(0)))
            by_account[row.account_id] = (debit + row.debit, credit + row.credit)
        # Nợ 131 = 1 375 000; Có 5111 = 1 250 000 (SAU chiết khấu); Có 33311 = 125 000.
        assert by_account[accounts["131"]] == (Decimal(1_375_000), Decimal(0))
        assert by_account[accounts["5111"]] == (Decimal(0), Decimal(1_250_000))
        assert by_account[accounts["33311"]] == (Decimal(0), Decimal(125_000))
        # Chiết khấu trừ thẳng trên dòng: không có bút toán giảm trừ doanh thu.
        assert accounts["521"] not in by_account
        # Bút toán chỉ chạm ĐÚNG ba TK ấy: không cặp giá vốn (phase 8), không
        # cặp giảm trừ doanh thu, không dòng nào khác lọt vào.
        assert set(by_account) == {accounts["131"], accounts["5111"], accounts["33311"]}

        receivable_rows = [row for row in postings if row.account_id == accounts["131"]]
        assert {(row.partner_kind, row.partner_id, row.debit) for row in receivable_rows} == {
            (PartnerKind.CUSTOMER.value, CUSTOMER_ID, Decimal(950_000)),
            (PartnerKind.CUSTOMER.value, CUSTOMER_ID, Decimal(300_000)),
            (PartnerKind.CUSTOMER.value, CUSTOMER_ID, Decimal(95_000)),
            (PartnerKind.CUSTOMER.value, CUSTOMER_ID, Decimal(30_000)),
        }
        # Dòng doanh thu mang chiều vật tư, không mang khách hàng.
        assert all(row.partner_id is None for row in postings if row.account_id == accounts["5111"])
        # Sổ quản trị chép y sổ tài chính khi mapper không đưa dòng riêng.
        assert len(_postings(session, voucher.id, Ledger.MANAGEMENT)) == len(postings)

        # Hóa đơn bán chỉ có MỘT người mua và một TK phải thu → một dòng sổ phụ.
        (debt,) = _subledger_rows(session, voucher.id)
        assert (debt.partner_id, debt.account_id) == (CUSTOMER_ID, accounts["131"])
        assert (debt.amount_fc, debt.amount) == (Decimal(1_375_000), Decimal(1_375_000))
        assert debt.target_kind == SettlementTargetKind.SALES_INVOICE.value
        assert debt.partner_kind == PartnerKind.CUSTOMER.value
        assert debt.ledger == 0

        service.unpost(voucher.id, user_id=ACTOR_ID)
        assert _subledger_rows(session, voucher.id) == []
        assert _postings(session, voucher.id) == []
        return None

    run(work)


def test_foreign_currency_subledger_matches_ledger_per_line_rounding(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tỷ giá lẻ: engine làm tròn TỪNG dòng, sổ phụ phải cộng cùng cách ấy."""

    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        rate = Decimal("25123.456")
        lines = tuple(
            SalesInvoiceLineIn(
                description=f"USD {label}",
                amount_fc=amount,
                account_id=accounts["5111"],
            )
            for label, amount in (
                ("A", Decimal("10.01")),
                ("B", Decimal("20.02")),
                ("C", Decimal("0.33")),
            )
        )
        voucher = service.create(
            _sales_invoice(context, accounts, currency_code="USD", exchange_rate=rate, lines=lines),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        ledger_debit = sum(
            (
                row.debit
                for row in _postings(session, voucher.id)
                if row.account_id == accounts["131"]
            ),
            Decimal(0),
        )
        (entry,) = _subledger_rows(session, voucher.id)
        assert entry.amount_fc == Decimal("30.36")
        assert entry.amount == ledger_debit
        assert entry.currency_code == "USD"
        assert entry.exchange_rate == rate
        return None

    run(work)


def test_due_date_falls_back_to_the_customer_payment_term(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-SAL-009 "điều khoản tự lấy theo khách hàng": chứng từ không chọn điều
    khoản thì server lấy điều khoản của khách; điều khoản trên chứng từ thắng
    danh mục, và hạn client gửi thắng cả hai."""

    def work(session: Session) -> object:
        customer_term = ensure_payment_term(session, term_id=9204, code="NET30-7C2", due_days=30)
        document_term = ensure_payment_term(session, term_id=9205, code="NET07-7C2", due_days=7)
        ensure_customer(
            session,
            partner_id=TERM_CUSTOMER_ID,
            code="KH-7C2-TERM",
            payment_term_id=customer_term,
        )
        service = SalesInvoiceService(session)

        from_catalog = _sales_invoice(context, accounts, customer_id=TERM_CUSTOMER_ID)
        voucher = service.create(from_catalog, user_id=ACTOR_ID)
        _, body, _, _ = service.get(voucher.id)
        assert body.due_date == date(2026, 2, 14)

        on_document = from_catalog.model_copy(update={"payment_term_id": document_term})
        voucher = service.create(on_document, user_id=ACTOR_ID)
        _, body, _, _ = service.get(voucher.id)
        assert body.due_date == date(2026, 1, 22)

        explicit = on_document.model_copy(update={"due_date": date(2026, 3, 1)})
        voucher = service.create(explicit, user_id=ACTOR_ID)
        _, body, _, _ = service.get(voucher.id)
        assert body.due_date == date(2026, 3, 1)

        service.post(voucher.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, voucher.id)
        assert debt.due_date == date(2026, 3, 1)
        return None

    run(work)


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        (SalesInvoiceKind.RETURN, "tra-lai-hang-ban"),
        (SalesInvoiceKind.ALLOWANCE, "giam-gia-hang-ban"),
    ],
)
def test_reversing_documents_settle_the_original_and_reverse_entries(
    run: Runner, context: PostingContext, accounts: dict[str, int], kind: int, operation: str
) -> None:
    """Trả lại hàng và giảm giá hàng bán đi cùng một đường: đảo chiều bút toán,
    giảm nợ hóa đơn gốc, không sinh khoản nợ mới."""

    def work(session: Session) -> object:
        service, original, debt = _post_original(session, context, accounts)
        assert debt.amount_fc == Decimal(1_375_000)

        reversing_lines = (
            SalesInvoiceLineIn(
                description="Trả lại Hàng B",
                quantity=Decimal(5),
                unit_price_fc=Decimal(60_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["521"],
                vat_account_id=accounts["33311"],
            ),
        )
        reversing = service.create(
            _sales_invoice(
                context,
                accounts,
                kind=kind,
                operation=operation,
                posting_date=JAN_20,
                lines=reversing_lines,
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(330_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        _, _, _, settlements = service.get(reversing.id)
        assert [(row.amount_fc, row.amount, row.fx_diff) for row in settlements] == [
            (Decimal(330_000), Decimal(330_000), Decimal(0))
        ]

        service.post(reversing.id, user_id=ACTOR_ID)
        # Đảo chiều: Nợ 521 / Có 131, Nợ 33311 / Có 131.
        assert {
            (row.account_id, row.debit, row.credit) for row in _postings(session, reversing.id)
        } == {
            (accounts["521"], Decimal(300_000), Decimal(0)),
            (accounts["131"], Decimal(0), Decimal(300_000)),
            (accounts["33311"], Decimal(30_000), Decimal(0)),
            (accounts["131"], Decimal(0), Decimal(30_000)),
        }
        # Chứng từ giảm trừ không sinh khoản nợ mới; nó giảm khoản gốc.
        assert _subledger_rows(session, reversing.id) == []
        session.refresh(debt)
        assert debt.settled_fc == Decimal(330_000)
        assert debt.settled == Decimal(330_000)

        # Hóa đơn gốc đã bị đối trừ thì không bỏ ghi sổ được.
        with pytest.raises(PostingValidationError) as caught:
            service.unpost(original.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == SUBLEDGER_SETTLED_CODE

        service.unpost(reversing.id, user_id=ACTOR_ID)
        session.refresh(debt)
        assert debt.settled_fc == Decimal(0)
        service.unpost(original.id, user_id=ACTOR_ID)
        assert _subledger_rows(session, original.id) == []
        return None

    run(work)


def test_reversing_document_needs_a_settlement_target(
    context: PostingContext, accounts: dict[str, int]
) -> None:
    """Sổ phụ không có dòng âm, nên chứng từ giảm trừ không đối trừ vào đâu là
    một chứng từ không có chỗ để ghi — chặn ngay ở schema."""
    with pytest.raises(ValueError, match="đối trừ vào hóa đơn gốc"):
        _sales_invoice(
            context, accounts, kind=SalesInvoiceKind.RETURN, operation="tra-lai-hang-ban"
        )


def test_reversing_a_fully_settled_invoice_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Quyết định user 2026-09-04: hóa đơn gốc đã thu đủ thì không còn gì để
    đối trừ, và chứng từ trả lại bị từ chối thay vì sinh một khoản phải trả
    khách hàng đứng riêng — đường đúng lúc ấy là trả tiền lại bằng phiếu chi.

    Đây là ca 7A để mở và 7C-2 đóng, nên nó được ghim bằng một bài riêng: nếu
    ai đó sau này mở đường sinh khoản mới, bài này đỏ trước khi sổ phụ có dòng
    đầu tiên mà chiều nợ không diễn đạt được.
    """

    def work(session: Session) -> object:
        service, _, debt = _post_original(session, context, accounts)
        # Thu đủ: chính đường mà một chứng từ giảm trừ khác đã dùng.
        paid_off = service.create(
            _sales_invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.RETURN,
                operation="tra-lai-hang-ban",
                posting_date=JAN_20,
                lines=(
                    SalesInvoiceLineIn(
                        description="Trả lại toàn bộ",
                        amount_fc=Decimal(1_250_000),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal(125_000),
                        account_id=accounts["521"],
                        vat_account_id=accounts["33311"],
                    ),
                ),
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(1_375_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(paid_off.id, user_id=ACTOR_ID)
        session.refresh(debt)
        assert debt.is_closed is True

        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _sales_invoice(
                    context,
                    accounts,
                    kind=SalesInvoiceKind.RETURN,
                    operation="tra-lai-hang-ban",
                    posting_date=JAN_20,
                    lines=(
                        SalesInvoiceLineIn(
                            description="Trả lại thêm",
                            amount_fc=Decimal(100_000),
                            account_id=accounts["521"],
                        ),
                    ),
                    settlements=(
                        SalesSettlementIn(
                            target_kind=SettlementTargetKind.SALES_INVOICE,
                            target_id=debt.id,
                            amount_fc=Decimal(100_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert SETTLEMENT_OVERPAID_CODE in {v.code for v in caught.value.violations}
        return None

    run(work)


def test_reversing_document_must_settle_debt_on_its_own_receivable_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn gốc treo nợ trên 1311, chứng từ trả lại ghi Có 131: đối trừ vào
    khoản ấy là sổ cái giảm 131 mà sổ phụ giảm 1311 — từ chối."""

    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        original = service.create(
            _sales_invoice(context, accounts).model_copy(
                update={"receivable_account_id": accounts["1311"]}
            ),
            user_id=ACTOR_ID,
        )
        service.post(original.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, original.id)
        assert debt.account_id == accounts["1311"]

        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _sales_invoice(
                    context,
                    accounts,
                    kind=SalesInvoiceKind.RETURN,
                    operation="tra-lai-hang-ban",
                    posting_date=JAN_20,
                    settlements=(
                        SalesSettlementIn(
                            target_kind=SettlementTargetKind.SALES_INVOICE,
                            target_id=debt.id,
                            amount_fc=Decimal(1_375_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert SETTLEMENT_ACCOUNT_MISMATCH_CODE in {v.code for v in caught.value.violations}
        return None

    run(work)


def test_foreign_currency_return_settles_exactly_what_it_credits(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tỷ giá lẻ: số VND ghi Có 131 của chứng từ trả lại **bằng đúng** số VND
    sổ phụ trừ khỏi hóa đơn gốc.

    Hai bên làm tròn theo hai cách chẻ khác nhau — sổ cái theo từng cặp
    hàng/thuế, đối trừ theo từng hóa đơn đích. Không dồn phần lẻ vào dòng đối
    trừ cuối thì hóa đơn gốc treo lại đúng phần lẻ ấy, không ai đối trừ được
    nữa. Đối xứng với bài cùng tên của chiều mua, đổi hướng lãi/lỗ tỷ giá:
    giảm phải thu cùng hướng với phiếu **thu**.
    """

    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        original_rate = Decimal("23000.7")
        original = service.create(
            _sales_invoice(
                context,
                accounts,
                currency_code="USD",
                exchange_rate=original_rate,
                lines=(
                    SalesInvoiceLineIn(
                        description="USD hàng",
                        amount_fc=Decimal("600.00"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("60.00"),
                        account_id=accounts["5111"],
                        vat_account_id=accounts["33311"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(original.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, original.id)

        return_rate = Decimal("25123.456")
        returned = service.create(
            _sales_invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.RETURN,
                operation="tra-lai-hang-ban",
                posting_date=JAN_20,
                currency_code="USD",
                exchange_rate=return_rate,
                lines=(
                    SalesInvoiceLineIn(
                        description="Trả lại lô 1",
                        amount_fc=Decimal("333.33"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("33.33"),
                        account_id=accounts["521"],
                        vat_account_id=accounts["33311"],
                    ),
                    SalesInvoiceLineIn(
                        description="Trả lại lô 2",
                        amount_fc=Decimal("199.99"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("16.67"),
                        account_id=accounts["521"],
                        vat_account_id=accounts["33311"],
                    ),
                ),
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal("583.32"),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)

        postings = _postings(session, returned.id)
        receivable = [row for row in postings if row.account_id == accounts["131"]]
        net_credit = sum((row.credit - row.debit for row in receivable), Decimal(0))
        session.refresh(debt)
        assert debt.settled == net_credit
        # Chính là ca lệch: gộp rồi mới quy đổi thì ra số khác.
        assert net_credit != convert_currency(Decimal("583.32"), return_rate, 2)
        return None

    run(work)


def test_update_rewrites_body_and_moves_usage_counters(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = SalesInvoiceService(session)
        before_customer = usage_count_of(
            session, entity_type=PARTNER_TABLE_NAME, entity_id=CUSTOMER_ID
        )
        before_seller = usage_count_of(
            session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=SALESPERSON_ID
        )
        voucher = service.create(_sales_invoice(context, accounts), user_id=ACTOR_ID)
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=CUSTOMER_ID)
            == before_customer + 1
        )
        assert (
            usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=SALESPERSON_ID)
            == before_seller + 1
        )

        without_seller = _sales_invoice(context, accounts).model_copy(
            update={"salesperson_id": None}
        )
        service.update(
            voucher.id,
            without_seller,
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )
        _, body, lines, _ = service.get(voucher.id)
        assert body.salesperson_id is None
        assert [line.line_no for line in lines] == [1, 2]
        assert (
            usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=SALESPERSON_ID)
            == before_seller
        )

        with pytest.raises(PostingValidationError) as caught:
            service.update(
                voucher.id,
                _sales_invoice(context, accounts, kind=SalesInvoiceKind.SERVICE),
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == KIND_IMMUTABLE_CODE

        service.delete(voucher.id)
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=CUSTOMER_ID)
            == before_customer
        )
        return None

    run(work)


def test_correct_sales_data_keeps_both_integrity_checks_clean(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hai check toàn vẹn cộng từ một `UNION` các bảng chứng từ, nên phân hệ mới
    phải nộp nhánh của mình vào cả hai:

    * `settlement_matches_subledger` (BR-QUY-02) đọc `ar_ap_ledger.settled` và
      đối chiếu với các bảng đối trừ. Thiếu nhánh `sales_settlements` thì **mỗi**
      lượt trả lại hàng bán là một dòng đỏ "sổ phụ có số, không có dòng đối trừ
      nào" — trên dữ liệu hoàn toàn đúng.
    * `usage_counter_accurate` (BR-SYS-02) đối chiếu `master_data_usage` với
      tham chiếu thật. `SalesInvoiceService` đếm khách hàng và nhân viên bán
      hàng, nên thiếu nhánh `sales_invoices` thì **mỗi** hóa đơn bán là một dòng
      đỏ. Nhân viên bán hàng còn là tham chiếu ĐẦU TIÊN tới `employees` từ một
      chứng từ.

    Bài này canh đúng ca ấy: dựng dữ liệu bán đúng (một hóa đơn đã ghi sổ + một
    chứng từ trả lại đã đối trừ) rồi đòi hai check **không** kể tên nó.
    """

    def work(session: Session) -> object:
        service, _, debt = _post_original(session, context, accounts)
        returned = service.create(
            _sales_invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.RETURN,
                operation="tra-lai-hang-ban",
                posting_date=JAN_20,
                lines=(
                    SalesInvoiceLineIn(
                        description="Trả lại một phần",
                        amount_fc=Decimal(300_000),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal(30_000),
                        account_id=accounts["521"],
                        vat_account_id=accounts["33311"],
                    ),
                ),
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(330_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)
        session.refresh(debt)
        assert debt.settled_fc == Decimal(330_000)

        settlement_check = run_check(
            session,
            check_of("settlement_matches_subledger"),
            branch_id=context.branch_id,
        )
        # Đọc theo id chứ không theo tổng: tệp khác dùng chung dataset có thể cố
        # ý để lại dòng lệch của nó, và một assert "tổng = 0" sẽ vỡ vì lý do
        # khác lý do bài này viết ra.
        assert str(debt.id) not in {str(row["target_id"]) for row in settlement_check.sample}

        usage_check = run_check(
            session, check_of("usage_counter_accurate"), branch_id=context.branch_id
        )
        flagged = {(row["entity_type"], row["entity_id"]) for row in usage_check.sample}
        assert ("partners", CUSTOMER_ID) not in flagged
        assert ("employees", SALESPERSON_ID) not in flagged
        return None

    run(work)
