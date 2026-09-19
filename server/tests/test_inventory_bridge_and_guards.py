"""Cầu mua/bán → phiếu kho, ba guard kho và mục kiểm khóa sổ (lát 8A).

* Ghi sổ hóa đơn mua có dòng qua kho → phiếu NK sinh + đã ghi sổ, `source_
  document_id` trỏ hóa đơn, movement `COSTED` bằng đúng `amount_fc +
  landed_cost_fc` (BR-STK-03: giá trị sổ kho = giá trị Nợ 156); bỏ ghi sổ hóa
  đơn → phiếu gỡ và xóa. Hóa đơn không `warehouse_id` → không sinh.
* Chứng từ bán "kiêm phiếu xuất kho" → phiếu XK với cặp TK giá vốn từ dòng, 0
  dòng GL; trả lại hàng bán → phiếu NK không giá.
* Phiếu sinh từ nguồn đứng yên khi nguồn còn ghi sổ (sửa / xóa / bỏ ghi sổ).
* Guard tồn âm (FR-STK-040) kêu **trên hóa đơn bán** ở mức `warn`, chặn ở
  `block`, tính **floor lùi ngày**; phiếu sinh không kêu lần hai. Guard tồn tối
  thiểu (FR-STK-041) và guard TK kho không phiếu (FR-STK-042) trên GLE.
* Khóa kỳ bị chặn khi còn movement chưa tính giá / dấu bẩn.
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
    GOODS_ITEM_ID,
    MAIN_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    create_goods_item,
    issue_payload,
    movements_of,
    receipt_payload,
    seed_inventory_package_data,
    set_system_setting,
)
from ket.kernel.config.catalog import (
    INVENTORY_ACCOUNT_NO_MOVEMENT_WARNING_KEY,
    STOCK_BELOW_MIN_WARNING_KEY,
    STOCK_NEGATIVE_WARNING_KEY,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    InventoryVoucherDerivedError,
    PeriodLockChecklistError,
    PostingValidationError,
)
from ket.kernel.master_data.models.item import Item
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.inventory.guards import (
    INVENTORY_ACCOUNT_NO_MOVEMENT_CODE,
    STOCK_BELOW_MIN_CODE,
    STOCK_NEGATIVE_CODE,
)
from ket.modules.inventory.lock_check import INVENTORY_COSTING_CHECK, ensure_inventory_costed
from ket.modules.inventory.models import (
    CostState,
    InventoryRecalcMark,
    InventoryVoucherKind,
    MovementDirection,
)
from ket.modules.inventory.service import InventoryVoucherService
from ket.modules.purchase.models import PurchaseInvoiceKind, VendorInvoiceStatus
from ket.modules.purchase.schemas import (
    LandedCostIn,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineIn,
    PurchaseSettlementIn,
)
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.contracts import GUARD_WARNING_DETAIL, LOCK_CHECKS, Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_vendor, seed_purchase_package_data
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
VENDOR_ID = 8201
CUSTOMER_ID = 8202
SALESPERSON_ID = 8203
MAY_5 = date(2026, 5, 5)
MAY_10 = date(2026, 5, 10)
MAY_20 = date(2026, 5, 20)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    codes = seed_inventory_package_data(session_factory, dataset_alpha, context)
    codes |= seed_purchase_package_data(session_factory, dataset_alpha, context)
    codes |= seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_vendor(session, partner_id=VENDOR_ID, code="NCC-8A-01")
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-8A-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-8A-01")
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
def _guards_off(run: Runner) -> None:
    """Mỗi bài tự bật mức guard nó cần; mặc định tắt để bài khác không dội."""

    def work(session: Session) -> None:
        for key in (
            STOCK_NEGATIVE_WARNING_KEY,
            STOCK_BELOW_MIN_WARNING_KEY,
            INVENTORY_ACCOUNT_NO_MOVEMENT_WARNING_KEY,
        ):
            set_system_setting(session, key, "none")

    run(work)


def _purchase(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date = MAY_10,
    with_warehouse: bool = True,
    kind: int = PurchaseInvoiceKind.GOODS,
    quantity: Decimal = Decimal(10),
    settlements: tuple[PurchaseSettlementIn, ...] = (),
    goods_account: str = "156",
    item_id: int = GOODS_ITEM_ID,
) -> PurchaseInvoiceIn:
    return PurchaseInvoiceIn(
        kind=kind,
        operation_code="tra-lai-hang-mua" if kind == PurchaseInvoiceKind.RETURN else "mua-hang-hoa",
        vendor_id=VENDOR_ID,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
        vendor_invoice_no="0000777",
        description="mua hàng qua kho",
        lines=(
            PurchaseInvoiceLineIn(
                description="Hàng A",
                item_id=item_id,
                unit_id=UNIT_PIECE_ID,
                warehouse_id=MAIN_WAREHOUSE_ID if with_warehouse else None,
                quantity=quantity,
                unit_price_fc=Decimal(100_000),
                amount_fc=Decimal(100_000) * quantity,
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(10_000) * quantity,
                account_id=accounts[goods_account],
                vat_account_id=accounts["1331"],
            ),
            # Dòng dịch vụ: không kho, không sinh dòng phiếu.
            PurchaseInvoiceLineIn(
                description="Vận chuyển",
                amount_fc=Decimal(50_000),
                vat_rate=Decimal(0),
                vat_amount_fc=Decimal(0),
                account_id=accounts["642"],
            ),
        ),
        settlements=settlements,
    )


def _sales(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date = MAY_10,
    quantity: Decimal = Decimal(3),
    is_stock_issue: bool = True,
    warehouse_id: int | None = MAIN_WAREHOUSE_ID,
    kind: int = SalesInvoiceKind.GOODS,
    settlements: tuple[SalesSettlementIn, ...] = (),
    adjusts_voucher_id: UUID | None = None,
    item_id: int = GOODS_ITEM_ID,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        operation_code="tra-lai-hang-ban" if kind == SalesInvoiceKind.RETURN else "ban-hang-hoa",
        adjusts_voucher_id=adjusts_voucher_id,
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000888",
        description="bán hàng kiêm xuất kho",
        is_stock_issue=is_stock_issue,
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                item_id=item_id,
                unit_id=UNIT_PIECE_ID,
                warehouse_id=warehouse_id,
                quantity=quantity,
                unit_price_fc=Decimal(150_000),
                amount_fc=Decimal(150_000) * quantity,
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(15_000) * quantity,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                cogs_account_id=accounts["632"],
                inventory_account_id=accounts["156"],
            ),
        ),
        settlements=settlements,
    )


def _generated(session: Session, source_id: UUID) -> list[Voucher]:
    return InventoryVoucherService(session).generated_for_source(source_id)


def _stock_receipt(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    quantity: Decimal,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
) -> UUID:
    service = InventoryVoucherService(session)
    voucher = service.create(
        receipt_payload(
            context,
            accounts,
            posting_date=posting_date,
            quantity=quantity,
            warehouse_id=warehouse_id,
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


# ----------------------------------------------------------- cầu từ mua hàng


def test_posting_a_purchase_with_a_warehouse_line_generates_a_posted_receipt(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        purchase = PurchaseInvoiceService(session)
        invoice = purchase.create(_purchase(context, accounts), user_id=ACTOR_ID)
        assert _generated(session, invoice.id) == []
        purchase.post(invoice.id, user_id=ACTOR_ID)

        (receipt,) = _generated(session, invoice.id)
        assert receipt.document_type == "NK"
        assert receipt.status == VoucherStatus.DA_GHI_SO
        assert receipt.source_document_id == invoice.id
        _, body, lines = InventoryVoucherService(session).get(receipt.id)
        assert body.kind == InventoryVoucherKind.RECEIPT
        assert body.operation_code == "nhap-mua-hang"
        assert body.partner_id == VENDOR_ID
        assert [
            (line.item_id, line.base_quantity, line.amount_fc, line.source_line_id is not None)
            for line in lines
        ] == [(GOODS_ITEM_ID, Decimal(10), Decimal("1000000.00"), True)]
        # Phiếu sinh từ mua không có bút toán riêng (Nợ 156 / Có 331 đã ở hóa đơn).
        assert (
            session.scalar(select(GlPosting.id).where(GlPosting.voucher_id == receipt.id)) is None
        )
        (movement,) = movements_of(session, receipt.id)
        assert movement.direction == MovementDirection.IN
        assert movement.cost_state == CostState.COSTED
        assert movement.amount == Decimal("1000000.00")
        assert movement.unit_cost == Decimal("100000.000000")

        purchase.unpost(invoice.id, user_id=ACTOR_ID)
        assert _generated(session, invoice.id) == []
        assert session.get(Voucher, receipt.id) is None

    run(work)


def test_landed_cost_flows_into_the_receipt_value(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        payload = _purchase(context, accounts).model_copy(
            update={
                "landed_costs": (
                    LandedCostIn(
                        description="Vận chuyển",
                        vendor_id=None,
                        credit_account_id=accounts["111"],
                        amount_fc=Decimal(30_000),
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                    ),
                )
            }
        )
        purchase = PurchaseInvoiceService(session)
        invoice = purchase.create(payload, user_id=ACTOR_ID)
        purchase.post(invoice.id, user_id=ACTOR_ID)
        (receipt,) = _generated(session, invoice.id)
        (movement,) = movements_of(session, receipt.id)
        # Chi phí mua phân bổ theo giá trị: dòng hàng 1.000.000 / (1.000.000 +
        # 50.000 dịch vụ) × 30.000 = 28.571,43 → giá trị nhập 1.028.571,43.
        assert movement.amount == Decimal("1028571.43")

    run(work)


def test_a_purchase_without_warehouse_generates_nothing(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        purchase = PurchaseInvoiceService(session)
        # TK 1568 không theo dõi kho — validator chiều không chặn, và phiếu
        # không sinh vì dòng thiếu kho: đúng nhóm "chưa nhập kho" của BFF.
        invoice = purchase.create(
            _purchase(context, accounts, with_warehouse=False, goods_account="1568"),
            user_id=ACTOR_ID,
        )
        purchase.post(invoice.id, user_id=ACTOR_ID)
        assert _generated(session, invoice.id) == []

    run(work)


def test_a_purchase_return_generates_an_uncosted_issue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        purchase = PurchaseInvoiceService(session)
        original = purchase.create(_purchase(context, accounts), user_id=ACTOR_ID)
        purchase.post(original.id, user_id=ACTOR_ID)
        debt = session.scalar(
            select(ArApLedgerEntry).where(
                ArApLedgerEntry.document_id == original.id,
                ArApLedgerEntry.target_kind == SettlementTargetKind.PURCHASE_INVOICE.value,
            )
        )
        assert debt is not None
        returned = purchase.create(
            _purchase(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                posting_date=MAY_20,
                quantity=Decimal(2),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(270_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        purchase.post(returned.id, user_id=ACTOR_ID)
        (issue,) = _generated(session, returned.id)
        assert issue.document_type == "XK"
        (movement,) = movements_of(session, issue.id)
        assert movement.direction == MovementDirection.OUT
        assert movement.cost_state == CostState.PENDING
        assert movement.quantity == Decimal(2)

    run(work)


# ------------------------------------------------------------ cầu từ bán hàng


def test_a_stock_issuing_sale_generates_an_issue_with_the_cogs_pair(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        _stock_receipt(session, context, accounts, posting_date=MAY_5, quantity=Decimal(50))
        sales = SalesInvoiceService(session)
        invoice = sales.create(_sales(context, accounts), user_id=ACTOR_ID)
        sales.post(invoice.id, user_id=ACTOR_ID)
        (issue,) = _generated(session, invoice.id)
        assert issue.document_type == "XK" and issue.status == VoucherStatus.DA_GHI_SO
        _, body, (line,) = InventoryVoucherService(session).get(issue.id)
        assert body.operation_code == "xuat-ban-hang"
        assert body.partner_id == CUSTOMER_ID
        assert (line.debit_account_id, line.credit_account_id) == (accounts["632"], accounts["156"])
        assert line.unit_cost_fc is None
        assert session.scalar(select(GlPosting.id).where(GlPosting.voucher_id == issue.id)) is None
        (movement,) = movements_of(session, issue.id)
        assert movement.direction == MovementDirection.OUT
        assert movement.cost_state == CostState.PENDING

        sales.unpost(invoice.id, user_id=ACTOR_ID)
        assert _generated(session, invoice.id) == []

    run(work)


def test_a_sale_without_the_stock_flag_generates_nothing(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        sales = SalesInvoiceService(session)
        invoice = sales.create(_sales(context, accounts, is_stock_issue=False), user_id=ACTOR_ID)
        sales.post(invoice.id, user_id=ACTOR_ID)
        assert _generated(session, invoice.id) == []

    run(work)


# ------------------------------------------------ phiếu sinh từ nguồn đứng yên


def test_a_generated_voucher_cannot_be_touched_while_its_source_is_posted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        purchase = PurchaseInvoiceService(session)
        invoice = purchase.create(_purchase(context, accounts), user_id=ACTOR_ID)
        purchase.post(invoice.id, user_id=ACTOR_ID)
        (receipt,) = _generated(session, invoice.id)
        inventory = InventoryVoucherService(session)
        with pytest.raises(InventoryVoucherDerivedError):
            inventory.unpost(receipt.id, user_id=ACTOR_ID)
        with pytest.raises(InventoryVoucherDerivedError):
            inventory.update(
                receipt.id,
                receipt_payload(context, accounts, posting_date=MAY_10),
                expected_row_version=receipt.row_version,
                user_id=ACTOR_ID,
            )
        with pytest.raises(InventoryVoucherDerivedError):
            inventory.delete(receipt.id)
        # Đường đúng: bỏ ghi sổ nguồn — hook gỡ phiếu qua chính guard ấy.
        purchase.unpost(invoice.id, user_id=ACTOR_ID)
        assert session.get(Voucher, receipt.id) is None

    run(work)


# ------------------------------------------------------------ guard tồn kho


def _fresh_item(session: Session, tag: str) -> int:
    return create_goods_item(session, f"HH-8A-{tag}-{uuid4().hex[:6].upper()}")


def _stock_receipt_of(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date,
    quantity: Decimal,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
) -> UUID:
    service = InventoryVoucherService(session)
    voucher = service.create(
        receipt_payload(
            context,
            accounts,
            posting_date=posting_date,
            quantity=quantity,
            warehouse_id=warehouse_id,
            item_id=item_id,
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


def test_stock_negative_guard_warns_on_the_sales_invoice_and_not_twice(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "warn")
        item_id = _fresh_item(session, "WARN")  # chưa có tồn ở bất kỳ kho nào
        sales = SalesInvoiceService(session)
        invoice = sales.create(
            _sales(context, accounts, quantity=Decimal(4), item_id=item_id), user_id=ACTOR_ID
        )
        with pytest.raises(PostingValidationError) as caught:
            sales.post(invoice.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == STOCK_NEGATIVE_CODE
        assert violation.details[GUARD_WARNING_DETAIL] == 1
        assert violation.details["item_id"] == item_id
        assert Decimal(str(violation.details["projected"])) == Decimal(-4)
        # Chưa sinh gì: lượt ghi sổ đổ trước hook.
        assert _generated(session, invoice.id) == []
        # Xác nhận trên hóa đơn → phiếu sinh ghi sổ được dù cùng guard.
        sales.post(invoice.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (issue,) = _generated(session, invoice.id)
        assert issue.status == VoucherStatus.DA_GHI_SO

    run(work)


def test_stock_negative_guard_blocks_and_uses_the_floor_after_the_posting_date(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "block")
        item_id = _fresh_item(session, "BLOCK")
        # Nhập 10 ngày 1/7, xuất 8 ngày 20/7 → tồn từ 20/7 là 2.
        _stock_receipt_of(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=date(2026, 7, 1),
            quantity=Decimal(10),
        )
        inventory = InventoryVoucherService(session)
        first = inventory.create(
            issue_payload(
                context,
                accounts,
                posting_date=date(2026, 7, 20),
                quantity=Decimal(8),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        inventory.post(first.id, user_id=ACTOR_ID)
        # Xuất 5 LÙI NGÀY về 10/7: tại 10/7 còn 10, nhưng từ 20/7 chỉ còn 2 → âm.
        backdated = inventory.create(
            issue_payload(
                context,
                accounts,
                posting_date=date(2026, 7, 10),
                quantity=Decimal(5),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            inventory.post(backdated.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (violation,) = caught.value.violations
        assert violation.code == STOCK_NEGATIVE_CODE
        assert GUARD_WARNING_DETAIL not in violation.details
        assert Decimal(str(violation.details["projected"])) == Decimal(-3)
        # Xuất 2 thì vừa đủ.
        fits = inventory.create(
            issue_payload(
                context,
                accounts,
                posting_date=date(2026, 7, 10),
                quantity=Decimal(2),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        inventory.post(fits.id, user_id=ACTOR_ID)

    run(work)


def test_stock_below_min_guard_reads_the_item_threshold(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, STOCK_BELOW_MIN_WARNING_KEY, "warn")
        item_id = _fresh_item(session, "MIN")
        item = session.get(Item, item_id)
        assert item is not None
        item.min_stock_qty = Decimal(20)
        session.flush()
        august = date(2026, 8, 3)
        _stock_receipt_of(
            session, context, accounts, item_id=item_id, posting_date=august, quantity=Decimal(25)
        )
        inventory = InventoryVoucherService(session)
        issue = inventory.create(
            issue_payload(
                context, accounts, posting_date=august, quantity=Decimal(10), item_id=item_id
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            inventory.post(issue.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == STOCK_BELOW_MIN_CODE
        assert Decimal(str(violation.details["min_stock_qty"])) == Decimal(20)
        assert Decimal(str(violation.details["projected"])) == Decimal(15)
        # Xuất 5 thì còn đúng ngưỡng — không kêu.
        exact = inventory.create(
            issue_payload(
                context, accounts, posting_date=august, quantity=Decimal(5), item_id=item_id
            ),
            user_id=ACTOR_ID,
        )
        inventory.post(exact.id, user_id=ACTOR_ID)

    run(work)


def test_inventory_account_without_movement_guard_flags_a_manual_journal(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, INVENTORY_ACCOUNT_NO_MOVEMENT_WARNING_KEY, "warn")
        journal = JournalVoucherService(session)
        voucher = journal.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=MAY_10,
                posting_date=MAY_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="ghi thẳng vào TK kho",
                lines=(
                    JournalLineIn(
                        account_id=accounts["156"],
                        debit_fc=Decimal(1_000),
                        credit_fc=Decimal(0),
                        item_id=GOODS_ITEM_ID,
                        warehouse_id=MAIN_WAREHOUSE_ID,
                    ),
                    JournalLineIn(
                        account_id=accounts["111"], debit_fc=Decimal(0), credit_fc=Decimal(1_000)
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            journal.post(voucher.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == INVENTORY_ACCOUNT_NO_MOVEMENT_CODE
        assert violation.details["account_codes"] == "156"
        journal.post(voucher.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        # Hóa đơn mua sinh phiếu thì guard im — cùng TK 156.
        purchase = PurchaseInvoiceService(session)
        invoice = purchase.create(_purchase(context, accounts), user_id=ACTOR_ID)
        purchase.post(invoice.id, user_id=ACTOR_ID)
        assert len(_generated(session, invoice.id)) == 1

    run(work)


# ------------------------------------------------------------- khóa sổ


def test_period_lock_check_refuses_uncosted_movements_and_is_registered(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Gọi thẳng mục kiểm (khóa kỳ thật đòi khóa tuần tự từ kỳ 1 trên dataset
    dùng chung) và khẳng định nó nằm trong `LOCK_CHECKS` — hai nửa của một cổng."""
    assert ensure_inventory_costed in LOCK_CHECKS.checks()

    def work(session: Session) -> None:
        november = date(2026, 11, 3)
        period = session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
                AccountingPeriod.start_date <= november,
                AccountingPeriod.end_date >= november,
            )
        )
        assert period is not None
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        item_id = _fresh_item(session, "LOCK")
        # Dấu bẩn của mọi tệp chạy trước trong chi nhánh này (dataset dùng
        # chung) — mục kiểm đúng nghĩa soi CẢ chi nhánh, nên dọn trước khi đo.
        for mark in session.scalars(select(InventoryRecalcMark)):
            session.delete(mark)
        session.flush()
        _stock_receipt_of(
            session, context, accounts, item_id=item_id, posting_date=november, quantity=Decimal(5)
        )
        # Chỉ có phiếu nhập đã tính giá: kỳ khóa được (về phía kho).
        ensure_inventory_costed(session, period, year)
        inventory = InventoryVoucherService(session)
        issue = inventory.create(
            issue_payload(
                context, accounts, posting_date=november, quantity=Decimal(1), item_id=item_id
            ),
            user_id=ACTOR_ID,
        )
        inventory.post(issue.id, user_id=ACTOR_ID)
        with pytest.raises(PeriodLockChecklistError) as caught:
            ensure_inventory_costed(session, period, year)
        assert caught.value.details["check"] == INVENTORY_COSTING_CHECK
        assert caught.value.details["uncosted_movements"] == 1

    run(work)
