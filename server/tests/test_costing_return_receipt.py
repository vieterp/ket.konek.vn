"""Hàng bán trả lại lấy giá từ lần xuất (FR-STK-004, lát 8C-1).

Dòng hóa đơn trả lại (kind `RETURN`) chỉ dòng bán gốc → phiếu nhập sinh trỏ
movement xuất của phiếu XK sinh từ dòng gốc (`source_movement_id` chiều nhập)
→ giá nhập = giá xuất, ngay lúc ghi sổ nếu lần xuất đã có giá, và engine chép
lại mỗi lượt (`return_in_legs.sql`) — tính lại giá bán TỰ cập nhật giá nhập trả
lại; bút toán Nợ 156 / Có 632 đi qua `repost` như giá vốn. Cộng luật hình thức
ở cả hai phía (sales: kind/khách/mã hàng/đã ghi sổ; inventory: không gõ giá,
nguồn phải là lần xuất cùng mã hàng).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    financial_postings,
    fresh_item,
    in_movement,
    marks_of_branch,
    out_movement,
    post_receipt,
    receipt_payload,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
)
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError, SalesLineReturnedError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.inventory.models import CostState, InventoryVoucherLine
from ket.modules.inventory.schemas import InventoryVoucherIn
from ket.modules.inventory.service import (
    RECEIPT_COST_CONFLICTS_SOURCE_CODE,
    RETURN_SOURCE_AFTER_RECEIPT_CODE,
    RETURN_SOURCE_MISMATCH_CODE,
    InventoryVoucherService,
)
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoice, SalesInvoiceKind, SalesInvoiceLine
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import (
    RETURNED_LINE_KIND_CODE,
    RETURNED_LINE_MISMATCH_CODE,
    SalesInvoiceService,
)
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data
from test_opening_balances_carry_forward import ensure_fiscal_year

pytestmark = pytest.mark.db

ACTOR_ID = 1
CUSTOMER_ID = 8402
OTHER_CUSTOMER_ID = 8403
SALESPERSON_ID = 8404
MAR_1, MAR_5, MAR_10, MAR_15, MAR_20 = (
    date(2026, 3, 1),
    date(2026, 3, 5),
    date(2026, 3, 10),
    date(2026, 3, 15),
    date(2026, 3, 20),
)
PRICE = Decimal(150_000)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    codes = seed_inventory_package_data(session_factory, dataset_alpha, context)
    codes |= seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-8C-01")
        ensure_customer(session, partner_id=OTHER_CUSTOMER_ID, code="KH-8C-02")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-8C-01")
    return codes


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


@pytest.fixture(autouse=True)
def _guard_off(run: Runner) -> None:
    run(lambda session: set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none"))


def _line(
    accounts: dict[str, int],
    *,
    item_id: int,
    quantity: Decimal,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    returned_line_id: UUID | None = None,
) -> SalesInvoiceLineIn:
    return SalesInvoiceLineIn(
        description="Hàng C",
        item_id=item_id,
        unit_id=UNIT_PIECE_ID,
        warehouse_id=warehouse_id,
        quantity=quantity,
        unit_price_fc=PRICE,
        amount_fc=PRICE * quantity,
        vat_rate=Decimal(10),
        vat_amount_fc=(PRICE * quantity) / 10,
        account_id=accounts["5111"],
        vat_account_id=accounts["33311"],
        cogs_account_id=accounts["632"],
        inventory_account_id=accounts["156"],
        returned_line_id=returned_line_id,
    )


def _sale(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date,
    quantity: Decimal = Decimal(10),
    customer_id: int = CUSTOMER_ID,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000891",
        description="bán hàng kiêm xuất kho (8C-1)",
        is_stock_issue=True,
        lines=(_line(accounts, item_id=item_id, quantity=quantity),),
    )


def _sold_line(session: Session, invoice_id: UUID) -> SalesInvoiceLine:
    (line,) = session.scalars(
        select(SalesInvoiceLine).where(SalesInvoiceLine.voucher_id == invoice_id)
    ).all()
    return line


def _return(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    sold_invoice_id: UUID,
    item_id: int,
    quantity: Decimal,
    posting_date: date,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    customer_id: int = CUSTOMER_ID,
    kind: int = SalesInvoiceKind.RETURN,
    returned_line_id: UUID | None = None,
) -> SalesInvoiceIn:
    """Chứng từ trả lại `quantity` của hóa đơn gốc, đối trừ đủ tổng tiền trả."""
    (debt,) = session.scalars(
        select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == sold_invoice_id)
    ).all()
    total = PRICE * quantity + (PRICE * quantity) / 10
    line = _line(
        accounts,
        item_id=item_id,
        quantity=quantity,
        warehouse_id=warehouse_id,
        returned_line_id=(
            returned_line_id
            if returned_line_id is not None
            else _sold_line(session, sold_invoice_id).id
        ),
    )
    return SalesInvoiceIn(
        kind=kind,
        operation_code="tra-lai-hang-ban" if kind == SalesInvoiceKind.RETURN else "ban-hang-hoa",
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000892",
        description="hàng bán trả lại (8C-1)",
        is_stock_issue=True,
        lines=(line,),
        settlements=(
            (
                SalesSettlementIn(
                    target_kind=SettlementTargetKind.SALES_INVOICE,
                    target_id=debt.id,
                    amount_fc=total,
                ),
            )
            if kind == SalesInvoiceKind.RETURN
            else ()
        ),
    )


def _sell(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date = MAR_10,
    quantity: Decimal = Decimal(10),
) -> UUID:
    sales = SalesInvoiceService(session)
    invoice = sales.create(
        _sale(context, accounts, item_id=item_id, posting_date=posting_date, quantity=quantity),
        user_id=ACTOR_ID,
    )
    sales.post(invoice.id, user_id=ACTOR_ID, acknowledged_warnings=True)
    return invoice.id


def _pairs(session: Session, voucher_id: UUID) -> list[tuple[int, Decimal, Decimal]]:
    return [
        (p.account_id, p.debit, p.credit)
        for p in financial_postings(session, voucher_id)
        if p.debit > 0 or p.credit > 0
    ]


def test_return_receipt_takes_the_issue_cost_and_follows_recalc(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """R 5/3 10@100 → SAL 10/3 bán 10 (engine: 100) → trả lại 4 ngày 15/3: NK
    sinh trỏ movement xuất, COSTED 400 ngay lúc ghi sổ, GL Nợ 156 / Có 632 = 400,
    `cogs_posted` của chứng từ trả lại lật. R lùi ngày 1/3 10@200 → job: giá bán
    150 → NK 600, GL ghi lại."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT1")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item)
        run_engine(session, context)
        (issue,) = InventoryVoucherService(session).generated_for_source(sold)
        assert out_movement(session, issue.id).unit_cost == Decimal("100.000000")

        sales = SalesInvoiceService(session)
        returned = sales.create(
            _return(
                session,
                context,
                accounts,
                sold_invoice_id=sold,
                item_id=item,
                quantity=Decimal(4),
                posting_date=MAR_15,
            ),
            user_id=ACTOR_ID,
        )
        sales.post(returned.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (receipt,) = InventoryVoucherService(session).generated_for_source(returned.id)
        moved = in_movement(session, receipt.id)
        assert moved.source_movement_id == out_movement(session, issue.id).id
        assert (moved.cost_state, moved.unit_cost, moved.amount) == (
            CostState.COSTED,
            Decimal("100.000000"),
            Decimal("400.00"),
        )
        assert _pairs(session, receipt.id) == [
            (accounts["156"], Decimal(400), Decimal(0)),
            (accounts["632"], Decimal(0), Decimal(400)),
        ]
        body = session.get(SalesInvoice, returned.id)
        assert body is not None and body.cogs_posted is True

        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_1,
            quantity=Decimal(10),
            unit_cost=Decimal(200),
        )
        result = run_engine(session, context)
        assert result.pending_left == 0
        assert out_movement(session, issue.id).unit_cost == Decimal("150.000000")
        moved = in_movement(session, receipt.id)
        assert (moved.cost_state, moved.amount) == (CostState.COSTED, Decimal("600.00"))
        assert _pairs(session, receipt.id) == [
            (accounts["156"], Decimal(600), Decimal(0)),
            (accounts["632"], Decimal(0), Decimal(600)),
        ]

    run(work)


def test_return_to_another_warehouse_and_before_the_issue_is_costed(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Chưa chạy engine khi trả lại → NK chờ giá (0 GL); trả về kho B. Job một
    lượt: xuất A có giá → vế nhập B chép giá (khóa B vào vòng sau) → COSTED, GL."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(250),
        )
        sold = _sell(session, context, accounts, item_id=item)
        sales = SalesInvoiceService(session)
        returned = sales.create(
            _return(
                session,
                context,
                accounts,
                sold_invoice_id=sold,
                item_id=item,
                quantity=Decimal(2),
                posting_date=MAR_15,
                warehouse_id=SECOND_WAREHOUSE_ID,
            ),
            user_id=ACTOR_ID,
        )
        sales.post(returned.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (receipt,) = InventoryVoucherService(session).generated_for_source(returned.id)
        moved = in_movement(session, receipt.id)
        assert (moved.cost_state, moved.warehouse_id) == (CostState.PENDING, SECOND_WAREHOUSE_ID)
        assert financial_postings(session, receipt.id) == []
        body = session.get(SalesInvoice, returned.id)
        assert body is not None and body.cogs_posted is False

        result = run_engine(session, context)
        assert result.pending_left == 0 and result.passes >= 2
        moved = in_movement(session, receipt.id)
        assert (moved.cost_state, moved.amount) == (CostState.COSTED, Decimal("500.00"))
        assert _pairs(session, receipt.id) == [
            (accounts["156"], Decimal(500), Decimal(0)),
            (accounts["632"], Decimal(0), Decimal(500)),
        ]
        session.refresh(body)
        assert body.cogs_posted is True

    run(work)


def test_return_in_the_next_year_gets_a_mark_when_the_issue_is_repriced(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bán 2026, trả lại 1/2027 (đã COSTED theo giá cũ). Nhập lùi ngày 2026 đổi
    giá bán → lượt 2026 để dấu bẩn 2027 tại ngày nhập trả lại; lượt tiếp chép giá
    mới."""

    def work(session: Session) -> None:
        ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        item = fresh_item(session, "RT3")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item)
        run_engine(session, context)
        sales = SalesInvoiceService(session)
        returned = sales.create(
            _return(
                session,
                context,
                accounts,
                sold_invoice_id=sold,
                item_id=item,
                quantity=Decimal(3),
                posting_date=date(2027, 1, 12),
            ),
            user_id=ACTOR_ID,
        )
        sales.post(returned.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (receipt,) = InventoryVoucherService(session).generated_for_source(returned.id)
        assert in_movement(session, receipt.id).amount == Decimal("300.00")

        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_1,
            quantity=Decimal(10),
            unit_cost=Decimal(300),
        )
        first = run_engine(session, context)
        assert "2026" in first.years
        # Khóa có movement năm sau (chính lần nhập trả lại) nên dấu năm sau kẹp về
        # ngày đầu năm — `LEAST` của hai câu mark_*_next_year.
        marks = [m for m in marks_of_branch(session, context.branch_id) if m.item_id == item]
        assert [m.from_date for m in marks] == [date(2027, 1, 1)]
        second = run_engine(session, context)
        assert second.years == ["2027"]
        assert in_movement(session, receipt.id).amount == Decimal("600.00")

    run(work)


def test_sales_side_rules_for_the_returned_line(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item = fresh_item(session, "RT4")
        other = fresh_item(session, "RT4b")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item)
        sales = SalesInvoiceService(session)
        # kind GOODS mang dòng gốc → từ chối.
        with pytest.raises(PostingValidationError) as wrong_kind:
            sales.create(
                _return(
                    session,
                    context,
                    accounts,
                    sold_invoice_id=sold,
                    item_id=item,
                    quantity=Decimal(1),
                    posting_date=MAR_15,
                    kind=SalesInvoiceKind.GOODS,
                ),
                user_id=ACTOR_ID,
            )
        assert wrong_kind.value.violations[0].code == RETURNED_LINE_KIND_CODE
        # Khác mã hàng / khác khách / dòng không tồn tại → lệch.
        for label, payload in (
            (
                "khác mã hàng",
                _return(
                    session,
                    context,
                    accounts,
                    sold_invoice_id=sold,
                    item_id=other,
                    quantity=Decimal(1),
                    posting_date=MAR_15,
                ),
            ),
            (
                "khác khách",
                _return(
                    session,
                    context,
                    accounts,
                    sold_invoice_id=sold,
                    item_id=item,
                    quantity=Decimal(1),
                    posting_date=MAR_15,
                    customer_id=OTHER_CUSTOMER_ID,
                ),
            ),
            (
                "dòng không tồn tại",
                _return(
                    session,
                    context,
                    accounts,
                    sold_invoice_id=sold,
                    item_id=item,
                    quantity=Decimal(1),
                    posting_date=MAR_15,
                    returned_line_id=uuid4(),
                ),
            ),
        ):
            with pytest.raises(PostingValidationError) as mismatch:
                sales.create(payload, user_id=ACTOR_ID)
            assert mismatch.value.violations[0].code == RETURNED_LINE_MISMATCH_CODE, label

    run(work)


def test_return_without_a_stock_issue_source_waits_for_a_price(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn gốc không kiêm xuất kho → không có lần xuất để lấy giá: NK sinh
    chờ giá như 8B, không lỗi."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT5")
        sales = SalesInvoiceService(session)
        payload = _sale(context, accounts, item_id=item, posting_date=MAR_10).model_copy(
            update={"is_stock_issue": False}
        )
        sold = sales.create(payload, user_id=ACTOR_ID)
        sales.post(sold.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        assert InventoryVoucherService(session).generated_for_source(sold.id) == []
        returned = sales.create(
            _return(
                session,
                context,
                accounts,
                sold_invoice_id=sold.id,
                item_id=item,
                quantity=Decimal(1),
                posting_date=MAR_15,
            ),
            user_id=ACTOR_ID,
        )
        sales.post(returned.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (receipt,) = InventoryVoucherService(session).generated_for_source(returned.id)
        moved = in_movement(session, receipt.id)
        assert (moved.cost_state, moved.source_movement_id) == (CostState.PENDING, None)
        line = session.scalar(
            select(InventoryVoucherLine).where(InventoryVoucherLine.voucher_id == receipt.id)
        )
        assert line is not None and line.source_movement_id is None

    run(work)


def test_manual_return_receipt_rules(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """NK gõ tay `nhap-hang-ban-tra-lai` chỉ lần xuất: gõ giá → xung đột; trỏ lần
    NHẬP hay khác mã hàng → lệch; đúng → chép giá xuất ngay."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT6")
        other = fresh_item(session, "RT6b")
        receipt_a = post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        post_receipt(
            session,
            context,
            accounts,
            item_id=other,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item)
        run_engine(session, context)
        (issue,) = InventoryVoucherService(session).generated_for_source(sold)
        issue_movement = out_movement(session, issue.id)
        service = InventoryVoucherService(session)

        def manual(
            source_id: int, *, item_id: int = item, price: Decimal | None = None
        ) -> InventoryVoucherIn:
            payload = receipt_payload(
                context,
                accounts,
                posting_date=MAR_20,
                quantity=Decimal(2),
                item_id=item_id,
                operation="nhap-hang-ban-tra-lai",
            )
            return payload.model_copy(
                update={
                    "partner_id": CUSTOMER_ID,
                    "partner_kind": PartnerKind.CUSTOMER,
                    "lines": tuple(
                        line.model_copy(
                            update={
                                "source_movement_id": source_id,
                                "unit_cost_fc": price,
                                "debit_account_id": accounts["156"],
                                "credit_account_id": accounts["632"],
                            }
                        )
                        for line in payload.lines
                    ),
                }
            )

        with pytest.raises(PostingValidationError) as priced:
            service.create(manual(issue_movement.id, price=Decimal(5)), user_id=ACTOR_ID)
        assert priced.value.violations[0].code == RECEIPT_COST_CONFLICTS_SOURCE_CODE
        with pytest.raises(PostingValidationError) as points_to_receipt:
            service.create(manual(in_movement(session, receipt_a).id), user_id=ACTOR_ID)
        assert points_to_receipt.value.violations[0].code == RETURN_SOURCE_MISMATCH_CODE
        with pytest.raises(PostingValidationError) as other_item:
            service.create(manual(issue_movement.id, item_id=other), user_id=ACTOR_ID)
        assert other_item.value.violations[0].code == RETURN_SOURCE_MISMATCH_CODE

        voucher = service.create(manual(issue_movement.id), user_id=ACTOR_ID)
        service.post(voucher.id, user_id=ACTOR_ID)
        moved = in_movement(session, voucher.id)
        assert (moved.cost_state, moved.amount) == (CostState.COSTED, Decimal("200.00"))
        assert _pairs(session, voucher.id) == [
            (accounts["156"], Decimal(200), Decimal(0)),
            (accounts["632"], Decimal(0), Decimal(200)),
        ]

    run(work)


def test_return_dated_before_the_sale_is_refused_on_both_sides(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review M-4: nhập trả lại đứng trước lần xuất là vòng giá co dần không hội
    tụ — `sales` từ chối dòng gốc muộn hơn chứng từ trả lại; NK gõ tay từ chối
    lần xuất muộn hơn phiếu."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT7")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item, posting_date=MAR_15)
        run_engine(session, context)
        sales = SalesInvoiceService(session)
        with pytest.raises(PostingValidationError) as too_early:
            sales.create(
                _return(
                    session,
                    context,
                    accounts,
                    sold_invoice_id=sold,
                    item_id=item,
                    quantity=Decimal(1),
                    posting_date=MAR_10,
                ),
                user_id=ACTOR_ID,
            )
        assert too_early.value.violations[0].code == RETURNED_LINE_MISMATCH_CODE
        (issue,) = InventoryVoucherService(session).generated_for_source(sold)
        payload = receipt_payload(
            context,
            accounts,
            posting_date=MAR_10,
            quantity=Decimal(1),
            item_id=item,
            operation="nhap-hang-ban-tra-lai",
        ).model_copy(update={"partner_id": CUSTOMER_ID, "partner_kind": PartnerKind.CUSTOMER})
        payload = payload.model_copy(
            update={
                "lines": tuple(
                    line.model_copy(
                        update={
                            "source_movement_id": out_movement(session, issue.id).id,
                            "unit_cost_fc": None,
                            "debit_account_id": accounts["156"],
                            "credit_account_id": accounts["632"],
                        }
                    )
                    for line in payload.lines
                )
            }
        )
        with pytest.raises(PostingValidationError) as manual:
            InventoryVoucherService(session).create(payload, user_id=ACTOR_ID)
        assert manual.value.violations[0].code == RETURN_SOURCE_AFTER_RECEIPT_CODE

    run(work)


def test_original_invoice_cannot_be_edited_while_a_return_points_at_it(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review M-5: hóa đơn gốc bỏ ghi sổ được (chưa có gì sinh) nhưng sửa/xóa bị
    guard chặn có tên chứng từ trả lại, không phải FK RESTRICT thô."""

    def work(session: Session) -> None:
        item = fresh_item(session, "RT8")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item,
            posting_date=MAR_5,
            quantity=Decimal(10),
            unit_cost=Decimal(100),
        )
        sold = _sell(session, context, accounts, item_id=item)
        sales = SalesInvoiceService(session)
        returned = sales.create(
            _return(
                session,
                context,
                accounts,
                sold_invoice_id=sold,
                item_id=item,
                quantity=Decimal(1),
                posting_date=MAR_15,
            ),
            user_id=ACTOR_ID,
        )
        sales.unpost(sold, user_id=ACTOR_ID)
        with pytest.raises(SalesLineReturnedError) as refused:
            sales.delete(sold)
        assert refused.value.details["count"] == 1
        sales.delete(returned.id)
        sales.delete(sold)

    run(work)
