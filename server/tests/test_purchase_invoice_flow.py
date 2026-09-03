"""Vòng đời hóa đơn mua hàng trên PostgreSQL thật (lát 7B).

Kiểm những bất biến RIÊNG của phân hệ mua — luật ghi sổ chung đã có
`test_posting_engine_flow.py`, sổ phụ công nợ có `test_ar_ap_ledger_service.py`:

* Cất cấp số `PUR{YY}-`; nghiệp vụ phải thuộc gói (FR-SYS-025) và phải là
  nghiệp vụ của nhà cung cấp; thuế GTGT chỉ khi đã có hóa đơn (BR-PUR-02).
* Chi phí mua hàng phân bổ vào từng dòng lúc cất; tổng trên thân hóa đơn
  cộng đủ hàng + thuế + chi phí.
* Mapper: Nợ TK hàng/thuế mang chiều vật tư, Có TK phải trả mang chiều NCC;
  chi phí mua hàng có NCC riêng thì Có vào TK của khoản đó với NCC đó.
* Ghi sổ → sổ phụ có đúng khoản phải trả (một dòng NCC chính, một dòng NCC
  dịch vụ); bỏ ghi sổ → gỡ sạch; số VND của sổ phụ khớp sổ cái từng đồng kể
  cả ngoại tệ.
* Trả lại hàng đối trừ vào hóa đơn gốc: bút toán đảo chiều, `settled` của
  khoản gốc tăng rồi giảm theo ghi sổ / bỏ ghi sổ; hóa đơn gốc đã bị đối trừ
  thì không bỏ ghi sổ được.
* Bộ đếm tham chiếu NCC nhích/lùi theo tạo–sửa–xóa (BR-SYS-02).
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
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.money import convert_currency
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.purchase.models import (
    LandedCostAllocation,
    PurchaseInvoiceKind,
    VendorInvoiceStatus,
)
from ket.modules.purchase.schemas import (
    LandedCostIn,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineIn,
    PurchaseSettlementIn,
)
from ket.modules.purchase.service import (
    KIND_IMMUTABLE_CODE,
    OPERATION_PARTNER_REQUIRED_CODE,
    OPERATION_UNKNOWN_CODE,
    PAYABLE_ACCOUNT_NOT_VENDOR_TRACKED_CODE,
    VAT_REQUIRES_INVOICE_CODE,
    PurchaseInvoiceService,
)
from ket.modules.receivables.ledger_service import SUBLEDGER_SETTLED_CODE
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.contracts import VoucherStatus
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.settlements import SETTLEMENT_ACCOUNT_MISMATCH_CODE
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_payment_term, ensure_vendor, seed_purchase_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)
JAN_20 = date(2026, 1, 20)

MAIN_VENDOR_ID = 9101
FREIGHT_VENDOR_ID = 9102


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    accounts = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_vendor(session, partner_id=MAIN_VENDOR_ID, code="NCC-7B-01")
        ensure_vendor(session, partner_id=FREIGHT_VENDOR_ID, code="NCC-7B-VC")
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


def _goods_invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    operation: str = "mua-hang-hoa",
    kind: int = PurchaseInvoiceKind.GOODS,
    vendor_invoice_status: int = VendorInvoiceStatus.RECEIVED,
    landed_costs: tuple[LandedCostIn, ...] = (),
    settlements: tuple[PurchaseSettlementIn, ...] = (),
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    posting_date: date = JAN_15,
    lines: tuple[PurchaseInvoiceLineIn, ...] | None = None,
) -> PurchaseInvoiceIn:
    return PurchaseInvoiceIn(
        kind=kind,
        operation_code=operation,
        vendor_id=MAIN_VENDOR_ID,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        vendor_invoice_status=vendor_invoice_status,
        vendor_invoice_no="0000123" if vendor_invoice_status == 0 else None,
        landed_cost_allocation=LandedCostAllocation.BY_VALUE,
        description="mua hàng test",
        lines=lines
        or (
            PurchaseInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(10),
                unit_price_fc=Decimal(100_000),
                amount_fc=Decimal(1_000_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(100_000),
                account_id=accounts["156"],
                vat_account_id=accounts["1331"],
            ),
            PurchaseInvoiceLineIn(
                description="Hàng B",
                quantity=Decimal(5),
                unit_price_fc=Decimal(60_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["156"],
                vat_account_id=accounts["1331"],
            ),
        ),
        landed_costs=landed_costs,
        settlements=settlements,
    )


def _freight(
    accounts: dict[str, int], amount: int = 130_000, *, vendor: int | None = None
) -> LandedCostIn:
    return LandedCostIn(
        description="Vận chuyển",
        vendor_id=vendor,
        credit_account_id=accounts["331"] if vendor is not None else accounts["111"],
        amount_fc=Decimal(amount),
        vat_rate=Decimal(0),
        vat_amount_fc=Decimal(0),
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


def test_create_numbers_allocates_costs_and_totals(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _goods_invoice(context, accounts, landed_costs=(_freight(accounts),)),
            user_id=ACTOR_ID,
        )
        assert voucher.voucher_no.startswith("PUR26-")
        assert voucher.document_type == "PUR"

        _, body, lines, costs, settlements = service.get(voucher.id)
        assert body.total_before_tax_fc == Decimal(1_300_000)
        assert body.total_vat_fc == Decimal(130_000)
        assert body.total_landed_cost_fc == Decimal(130_000)
        assert body.total_fc == Decimal(1_560_000)
        # 130 000 chia theo giá trị 1 000 000 : 300 000.
        assert [line.landed_cost_fc for line in lines] == [Decimal(100_000), Decimal(30_000)]
        assert [cost.line_no for cost in costs] == [1]
        assert settlements == []
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=MAIN_VENDOR_ID) == 1
        )
        return None

    run(work)


def test_operation_must_exist_and_belong_to_vendors(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        with pytest.raises(PostingValidationError) as unknown:
            service.create(
                _goods_invoice(context, accounts, operation="khong-co"), user_id=ACTOR_ID
            )
        assert unknown.value.violations[0].code == OPERATION_UNKNOWN_CODE

        with pytest.raises(PostingValidationError) as wrong_kind:
            service.create(
                _goods_invoice(context, accounts, operation="mua-cua-khach"), user_id=ACTOR_ID
            )
        assert wrong_kind.value.violations[0].code == OPERATION_PARTNER_REQUIRED_CODE
        return None

    run(work)


def test_vat_requires_a_received_vendor_invoice(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _goods_invoice(
                    context, accounts, vendor_invoice_status=VendorInvoiceStatus.NOT_YET
                ),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == VAT_REQUIRES_INVOICE_CODE

        # Không thuế thì "chưa có hóa đơn" là hợp lệ — hàng về trước, hóa đơn sau.
        no_vat = (
            PurchaseInvoiceLineIn(
                description="Hàng chưa hóa đơn",
                amount_fc=Decimal(500_000),
                account_id=accounts["156"],
            ),
        )
        voucher = service.create(
            _goods_invoice(
                context,
                accounts,
                vendor_invoice_status=VendorInvoiceStatus.NOT_YET,
                lines=no_vat,
            ),
            user_id=ACTOR_ID,
        )
        assert voucher.voucher_no.startswith("PUR26-")
        return None

    run(work)


def test_post_writes_ledger_and_subledger_then_unpost_clears(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _goods_invoice(
                context, accounts, landed_costs=(_freight(accounts, vendor=FREIGHT_VENDOR_ID),)
            ),
            user_id=ACTOR_ID,
        )
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO

        postings = _postings(session, voucher.id)
        by_account: dict[int, tuple[Decimal, Decimal]] = {}
        for row in postings:
            debit, credit = by_account.get(row.account_id, (Decimal(0), Decimal(0)))
            by_account[row.account_id] = (debit + row.debit, credit + row.credit)
        # Nợ 156 = hàng 1 300 000 + chi phí 130 000; Nợ 1331 = 130 000;
        # Có 331 = 1 430 000 (NCC chính) + 130 000 (NCC vận chuyển).
        assert by_account[accounts["156"]] == (Decimal(1_430_000), Decimal(0))
        assert by_account[accounts["1331"]] == (Decimal(130_000), Decimal(0))
        assert by_account[accounts["331"]] == (Decimal(0), Decimal(1_560_000))
        # Dòng Có 331 mang đúng NCC; dòng hàng mang chiều vật tư chứ không mang NCC.
        payable_rows = [row for row in postings if row.account_id == accounts["331"]]
        assert {(row.partner_kind, row.partner_id, row.credit) for row in payable_rows} == {
            # Hàng và thuế đi hai dòng riêng (Nợ 156 / Có 331, Nợ 1331 / Có 331)
            # để sổ cái đối ứng được từng TK; chi phí có NCC riêng đi dòng riêng.
            (PartnerKind.VENDOR.value, MAIN_VENDOR_ID, Decimal(1_000_000)),
            (PartnerKind.VENDOR.value, MAIN_VENDOR_ID, Decimal(300_000)),
            (PartnerKind.VENDOR.value, MAIN_VENDOR_ID, Decimal(100_000)),
            (PartnerKind.VENDOR.value, MAIN_VENDOR_ID, Decimal(30_000)),
            # Chi phí mua hàng cũng là cặp: bên Có NCC vận chuyển chẻ theo phần
            # phân bổ vào từng dòng hàng (100 000 + 30 000), không phải một
            # dòng 130 000 — hai vế mỗi cặp quy đổi giống nhau.
            (PartnerKind.VENDOR.value, FREIGHT_VENDOR_ID, Decimal(100_000)),
            (PartnerKind.VENDOR.value, FREIGHT_VENDOR_ID, Decimal(30_000)),
        }
        assert all(row.partner_id is None for row in postings if row.account_id == accounts["156"])
        # Sổ quản trị chép y sổ tài chính khi mapper không đưa dòng riêng.
        assert len(_postings(session, voucher.id, Ledger.MANAGEMENT)) == len(postings)

        rows = _subledger_rows(session, voucher.id)
        assert [(row.partner_id, row.account_id, row.amount_fc, row.amount) for row in rows] == [
            (MAIN_VENDOR_ID, accounts["331"], Decimal(1_430_000), Decimal(1_430_000)),
            (FREIGHT_VENDOR_ID, accounts["331"], Decimal(130_000), Decimal(130_000)),
        ]
        assert {row.target_kind for row in rows} == {SettlementTargetKind.PURCHASE_INVOICE.value}
        assert {row.ledger for row in rows} == {0}
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=FREIGHT_VENDOR_ID)
            == 1
        )

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
        service = PurchaseInvoiceService(session)
        rate = Decimal("25123.456")
        lines = (
            PurchaseInvoiceLineIn(
                description="USD A",
                amount_fc=Decimal("10.01"),
                account_id=accounts["156"],
            ),
            PurchaseInvoiceLineIn(
                description="USD B",
                amount_fc=Decimal("20.02"),
                account_id=accounts["156"],
            ),
            PurchaseInvoiceLineIn(
                description="USD C",
                amount_fc=Decimal("0.33"),
                account_id=accounts["156"],
            ),
        )
        voucher = service.create(
            _goods_invoice(context, accounts, currency_code="USD", exchange_rate=rate, lines=lines),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        ledger_credit = sum(
            (
                row.credit
                for row in _postings(session, voucher.id)
                if row.account_id == accounts["331"]
            ),
            Decimal(0),
        )
        (entry,) = _subledger_rows(session, voucher.id)
        assert entry.amount_fc == Decimal("30.36")
        assert entry.amount == ledger_credit
        assert entry.currency_code == "USD"
        assert entry.exchange_rate == rate
        return None

    run(work)


def test_foreign_currency_landed_costs_post_balanced_pairs(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tỷ giá lẻ + chi phí mua hàng: ba dòng bằng nhau nhận 33.34/33.33/33.33
    của khoản 100 — `Σ round(phần × 23000.5)` = 2 300 051 ≠ round(100 × 23000.5)
    = 2 300 050. Ghi "một Có tổng khoản + ba Nợ phần phân bổ" là lệch 1 đồng và
    engine từ chối; ghi thành cặp thì cân, và sổ phụ NCC vận chuyển khớp sổ
    cái từng đồng."""

    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        rate = Decimal("23000.5")
        lines = tuple(
            PurchaseInvoiceLineIn(
                description=f"USD {index}", amount_fc=Decimal(100), account_id=accounts["156"]
            )
            for index in range(3)
        )
        voucher = service.create(
            _goods_invoice(
                context,
                accounts,
                currency_code="USD",
                exchange_rate=rate,
                lines=lines,
                landed_costs=(_freight(accounts, 100, vendor=FREIGHT_VENDOR_ID),),
            ),
            user_id=ACTOR_ID,
        )
        _, _, stored_lines, _, _ = service.get(voucher.id)
        assert [line.landed_cost_fc for line in stored_lines] == [
            Decimal("33.34"),
            Decimal("33.33"),
            Decimal("33.33"),
        ]
        service.post(voucher.id, user_id=ACTOR_ID)

        postings = _postings(session, voucher.id)
        assert sum((row.debit for row in postings), Decimal(0)) == sum(
            (row.credit for row in postings), Decimal(0)
        )
        freight_credit = sum(
            (row.credit for row in postings if row.partner_id == FREIGHT_VENDOR_ID), Decimal(0)
        )
        # Chính là ca lệch: tổng ba phần đã quy đổi khác `round(100 × tỷ giá)`,
        # nên "một Có tổng khoản" sẽ không cân với ba Nợ.
        assert freight_credit != Decimal(100) * rate
        # Mỗi mẩu là một cặp: bên Có của NCC vận chuyển chẻ đúng như bên Nợ 156.
        assert sorted(row.credit_fc for row in postings if row.partner_id == FREIGHT_VENDOR_ID) == [
            Decimal("33.33"),
            Decimal("33.33"),
            Decimal("33.34"),
        ]
        freight_row = next(
            row
            for row in _subledger_rows(session, voucher.id)
            if row.partner_id == FREIGHT_VENDOR_ID
        )
        assert freight_row.amount_fc == Decimal(100)
        assert freight_row.amount == freight_credit
        return None

    run(work)


def test_vendor_debt_must_sit_on_a_vendor_tracked_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Khoản chi phí kèm NCC ghi Có 111 (không theo dõi NCC) sẽ thành một dòng
    sổ phụ mà sổ cái không có — từ chối từ lúc cất."""

    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        paid_in_cash_but_with_vendor = LandedCostIn(
            description="Bốc xếp",
            vendor_id=FREIGHT_VENDOR_ID,
            credit_account_id=accounts["111"],
            amount_fc=Decimal(50_000),
            vat_rate=Decimal(0),
            vat_amount_fc=Decimal(0),
        )
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _goods_invoice(context, accounts, landed_costs=(paid_in_cash_but_with_vendor,)),
                user_id=ACTOR_ID,
            )
        (violation,) = caught.value.violations
        assert violation.code == PAYABLE_ACCOUNT_NOT_VENDOR_TRACKED_CODE
        assert violation.details["account_id"] == accounts["111"]

        payload = _goods_invoice(context, accounts).model_copy(
            update={"payable_account_id": accounts["111"]}
        )
        with pytest.raises(PostingValidationError) as caught:
            service.create(payload, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == PAYABLE_ACCOUNT_NOT_VENDOR_TRACKED_CODE
        return None

    run(work)


def test_due_date_comes_from_the_payment_term_when_not_sent(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-PUR-034: hạn thanh toán = ngày chứng từ + `due_days`; client gửi hạn
    thì hạn ấy thắng."""

    def work(session: Session) -> object:
        term_id = ensure_payment_term(session, term_id=9103, code="NET30-7B", due_days=30)
        service = PurchaseInvoiceService(session)
        payload = _goods_invoice(context, accounts).model_copy(update={"payment_term_id": term_id})
        voucher = service.create(payload, user_id=ACTOR_ID)
        _, body, _, _, _ = service.get(voucher.id)
        assert body.due_date == date(2026, 2, 14)

        explicit = payload.model_copy(update={"due_date": date(2026, 3, 1)})
        voucher = service.create(explicit, user_id=ACTOR_ID)
        _, body, _, _, _ = service.get(voucher.id)
        assert body.due_date == date(2026, 3, 1)

        service.post(voucher.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, voucher.id)
        assert debt.due_date == date(2026, 3, 1)
        return None

    run(work)


def test_return_must_settle_debt_on_its_own_payable_account(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn gốc treo nợ trên 3388, chứng từ trả lại ghi Nợ 331: đối trừ vào
    khoản ấy là sổ cái giảm 331 mà sổ phụ giảm 3388 — từ chối."""

    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        original = service.create(
            _goods_invoice(context, accounts).model_copy(
                update={"payable_account_id": accounts["3388"]}
            ),
            user_id=ACTOR_ID,
        )
        service.post(original.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, original.id)
        assert debt.account_id == accounts["3388"]

        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _goods_invoice(
                    context,
                    accounts,
                    kind=PurchaseInvoiceKind.RETURN,
                    operation="tra-lai-hang-mua",
                    posting_date=JAN_20,
                    settlements=(
                        PurchaseSettlementIn(
                            target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                            target_id=debt.id,
                            amount_fc=Decimal(1_430_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert SETTLEMENT_ACCOUNT_MISMATCH_CODE in {v.code for v in caught.value.violations}
        return None

    run(work)


def test_return_settles_the_original_invoice_and_reverses_entries(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        original = service.create(_goods_invoice(context, accounts), user_id=ACTOR_ID)
        service.post(original.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, original.id)
        assert debt.amount_fc == Decimal(1_430_000)

        return_lines = (
            PurchaseInvoiceLineIn(
                description="Trả lại Hàng B",
                quantity=Decimal(5),
                unit_price_fc=Decimal(60_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["156"],
                vat_account_id=accounts["1331"],
            ),
        )
        returned = service.create(
            _goods_invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                posting_date=JAN_20,
                lines=return_lines,
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(330_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        _, _, _, _, settlements = service.get(returned.id)
        assert [(row.amount_fc, row.amount, row.fx_diff) for row in settlements] == [
            (Decimal(330_000), Decimal(330_000), Decimal(0))
        ]

        service.post(returned.id, user_id=ACTOR_ID)
        postings = _postings(session, returned.id)
        # Đảo chiều: Nợ 331 / Có 156, Nợ 331 / Có 1331.
        assert {(row.account_id, row.debit, row.credit) for row in postings} == {
            (accounts["331"], Decimal(300_000), Decimal(0)),
            (accounts["156"], Decimal(0), Decimal(300_000)),
            (accounts["331"], Decimal(30_000), Decimal(0)),
            (accounts["1331"], Decimal(0), Decimal(30_000)),
        }
        # Trả lại hàng không sinh khoản nợ mới; nó giảm khoản gốc.
        assert _subledger_rows(session, returned.id) == []
        session.refresh(debt)
        assert debt.settled_fc == Decimal(330_000)
        assert debt.settled == Decimal(330_000)

        # Hóa đơn gốc đã bị đối trừ thì không bỏ ghi sổ được.
        with pytest.raises(PostingValidationError) as caught:
            service.unpost(original.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == SUBLEDGER_SETTLED_CODE

        service.unpost(returned.id, user_id=ACTOR_ID)
        session.refresh(debt)
        assert debt.settled_fc == Decimal(0)
        service.unpost(original.id, user_id=ACTOR_ID)
        assert _subledger_rows(session, original.id) == []
        return None

    run(work)


def test_foreign_currency_return_settles_exactly_what_it_debits(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tỷ giá lẻ: số VND ghi Nợ 331 của chứng từ trả lại **bằng đúng** số VND
    sổ phụ trừ khỏi hóa đơn gốc.

    Hai bên làm tròn theo hai cách chẻ khác nhau — sổ cái theo từng cặp
    hàng/thuế, đối trừ theo từng hóa đơn đích — nên `Σ round(phần × tỷ giá)`
    lệch `round(tổng × tỷ giá)` 1 xu ở bộ số này. Không dồn phần lẻ vào dòng
    đối trừ cuối thì hóa đơn gốc treo lại đúng phần lẻ ấy, không ai đối trừ
    được nữa.
    """

    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        original_rate = Decimal("23000.7")
        original = service.create(
            _goods_invoice(
                context,
                accounts,
                currency_code="USD",
                exchange_rate=original_rate,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="USD hàng",
                        amount_fc=Decimal("600.00"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("60.00"),
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(original.id, user_id=ACTOR_ID)
        (debt,) = _subledger_rows(session, original.id)

        return_rate = Decimal("25123.456")
        returned = service.create(
            _goods_invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                posting_date=JAN_20,
                currency_code="USD",
                exchange_rate=return_rate,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Trả lại lô 1",
                        amount_fc=Decimal("333.33"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("33.33"),
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                    PurchaseInvoiceLineIn(
                        description="Trả lại lô 2",
                        amount_fc=Decimal("199.99"),
                        vat_rate=Decimal(10),
                        vat_amount_fc=Decimal("16.67"),
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                ),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal("583.32"),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)

        postings = _postings(session, returned.id)
        payable = [row for row in postings if row.account_id == accounts["331"]]
        net_debit = sum((row.debit - row.credit for row in payable), Decimal(0))
        session.refresh(debt)
        assert debt.settled == net_debit
        # Chính là ca lệch: gộp rồi mới quy đổi thì ra số khác.
        assert net_debit != convert_currency(Decimal("583.32"), return_rate, 2)
        return None

    run(work)


def test_landed_cost_of_the_invoice_vendor_joins_its_subledger_row(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Khoản chi phí do CHÍNH nhà cung cấp hóa đơn thu, ghi Có đúng TK phải trả
    của hóa đơn → **một** dòng sổ phụ, không hai: sổ phụ mỗi (chứng từ, đối
    tác, TK) một dòng, hai dòng trùng khóa ấy làm màn đối trừ liệt kê hóa đơn
    thành hai khoản."""

    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _goods_invoice(
                context,
                accounts,
                landed_costs=(_freight(accounts, 130_000, vendor=MAIN_VENDOR_ID),),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        rows = _subledger_rows(session, voucher.id)
        assert len(rows) == 1
        # 1 430 000 của hàng + thuế, cộng 130 000 chi phí (không thuế).
        assert rows[0].amount_fc == Decimal(1_560_000)
        assert rows[0].account_id == accounts["331"]
        postings = _postings(session, voucher.id)
        payable_credit = sum(
            (row.credit for row in postings if row.account_id == accounts["331"]), Decimal(0)
        )
        assert rows[0].amount == payable_credit
        return None

    run(work)


def test_update_rewrites_body_and_moves_usage_counters(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = PurchaseInvoiceService(session)
        before_main = usage_count_of(
            session, entity_type=PARTNER_TABLE_NAME, entity_id=MAIN_VENDOR_ID
        )
        before_freight = usage_count_of(
            session, entity_type=PARTNER_TABLE_NAME, entity_id=FREIGHT_VENDOR_ID
        )
        voucher = service.create(_goods_invoice(context, accounts), user_id=ACTOR_ID)
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=MAIN_VENDOR_ID)
            == before_main + 1
        )

        service.update(
            voucher.id,
            _goods_invoice(
                context, accounts, landed_costs=(_freight(accounts, vendor=FREIGHT_VENDOR_ID),)
            ),
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )
        _, body, lines, costs, _ = service.get(voucher.id)
        assert body.total_landed_cost_fc == Decimal(130_000)
        assert [line.landed_cost_fc for line in lines] == [Decimal(100_000), Decimal(30_000)]
        assert [cost.vendor_id for cost in costs] == [FREIGHT_VENDOR_ID]
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=FREIGHT_VENDOR_ID)
            == before_freight + 1
        )

        with pytest.raises(PostingValidationError) as caught:
            service.update(
                voucher.id,
                _goods_invoice(context, accounts, kind=PurchaseInvoiceKind.SERVICE),
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == KIND_IMMUTABLE_CODE

        service.delete(voucher.id)
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=MAIN_VENDOR_ID)
            == before_main
        )
        assert (
            usage_count_of(session, entity_type=PARTNER_TABLE_NAME, entity_id=FREIGHT_VENDOR_ID)
            == before_freight
        )
        return None

    run(work)
