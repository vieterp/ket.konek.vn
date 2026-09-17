"""Ba văn bản gửi đối tác in từ sổ (lát 7G-5) — `POST /api/v1/partners/{id}/documents/*/print`.

Bất biến của biên bản đối chiếu theo kỳ: `đầu + tăng − giảm = cuối`, và mỗi số
đến đúng từ chỗ của nó — đầu kỳ là nợ còn treo tại ngày liền trước kỳ (gồm nợ
mang sang), tăng là chứng từ có ngày trong kỳ, giảm là đối trừ GHI SỔ trong kỳ
(một lượt trả ở kỳ sau không được kéo về), cuối là còn treo ở cuối kỳ. Thông
báo công nợ đọc cùng nguồn với thẻ công nợ (7G-4). Quyền theo chiều, và
"bên B" phải là đúng đối tác.

Bộ gieo chỉ đi qua service thật (hóa đơn bán/mua ghi sổ, chứng từ trả lại đối
trừ, số dư ban đầu), không chạm `ar_ap_ledger` bằng tay.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account
from cash_book_support import seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.formatting import format_money
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.purchase.models import LandedCostAllocation, PurchaseInvoiceKind
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit, ensure_vendor, seed_purchase_package_data
from sales_support import ensure_customer, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
FULL_ROLE = "in_van_ban_doi_tac_du_quyen"
SALES_ONLY_ROLE = "in_van_ban_doi_tac_chi_ban"
PARTNER_ONLY_ROLE = "in_van_ban_doi_tac_chi_danh_muc"

AUG_10 = date(2026, 8, 10)
AUG_31 = date(2026, 8, 31)
SEP_01 = date(2026, 9, 1)
SEP_05 = date(2026, 9, 5)
SEP_10 = date(2026, 9, 10)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)
OCT_05 = date(2026, 10, 5)
OCT_15 = date(2026, 10, 15)
OPENING_DUE = date(2026, 2, 20)

# Khối id 949x: `dataset_alpha` dùng chung cả phiên, mỗi tệp một khối riêng
# (bẫy thứ tự tệp, xem 7G-4).
CUSTOMER_ID = 9491
VENDOR_ID = 9492
UNIT_ID = 9493
ITEM_ID = 9494
CUSTOMER_CODE = "KH-7G5-01"
VENDOR_CODE = "NCC-7G5-01"
BANK_ACCOUNT_CODE = "0123456789-7G5"

GOODS = Decimal(1_000_000)
VAT = Decimal(100_000)
TOTAL = GOODS + VAT
RETURN_GOODS = Decimal(200_000)
RETURN_VAT = Decimal(20_000)
RETURN_TOTAL = RETURN_GOODS + RETURN_VAT
LATE_RETURN_GOODS = Decimal(100_000)
LATE_RETURN_VAT = Decimal(10_000)
OPENING_AMOUNT = Decimal(500_000)

# Kỳ 09/2026: hóa đơn A (tháng 8, hạn 20/09) mang sang; hóa đơn B lập trong kỳ;
# trả lại một phần A trong kỳ; trả lại tiếp ở tháng 10 — KHÔNG thuộc kỳ.
EXPECTED_OPENING = TOTAL + OPENING_AMOUNT
EXPECTED_INCREASE = TOTAL
EXPECTED_DECREASE = RETURN_TOTAL
EXPECTED_CLOSING = EXPECTED_OPENING + EXPECTED_INCREASE - EXPECTED_DECREASE


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    codes = seed_sales_package_data(session_factory, dataset_alpha, context)
    codes |= seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code=CUSTOMER_CODE)
        ensure_vendor(session, partner_id=VENDOR_ID, code=VENDOR_CODE)
        ensure_unit(session, unit_id=UNIT_ID, code="Cai-7G5")
        ensure_item(session, item_id=ITEM_ID, code="VT-7G5", unit_id=UNIT_ID)
    ensure_company_bank_account(session_factory, dataset_alpha, context, code=BANK_ACCOUNT_CODE)
    return codes


def _sales_invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    due_date: date | None,
    goods: Decimal = GOODS,
    vat: Decimal = VAT,
    kind: int = SalesInvoiceKind.GOODS,
    operation: str = "ban-hang-hoa",
    settlements: tuple[SalesSettlementIn, ...] = (),
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        operation_code=operation,
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="văn bản đối tác 7G-5",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng 7G-5",
                item_id=ITEM_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(1),
                unit_price_fc=goods,
                amount_fc=goods,
                vat_rate=Decimal(10),
                vat_amount_fc=vat,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
            ),
        ),
        settlements=settlements,
    )


def _return_against(
    session: Session,
    service: SalesInvoiceService,
    context: PostingContext,
    accounts: dict[str, int],
    target_voucher: UUID,
    *,
    goods: Decimal,
    vat: Decimal,
    posting_date: date,
) -> UUID:
    """Chứng từ trả lại đối trừ hóa đơn gốc — đích là DÒNG SỔ PHỤ (7A)."""
    entry = session.execute(
        select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == target_voucher)
    ).scalar_one()
    voucher = service.create(
        _sales_invoice(
            context,
            accounts,
            posting_date=posting_date,
            due_date=None,
            goods=goods,
            vat=vat,
            kind=SalesInvoiceKind.RETURN,
            operation="tra-lai-hang-ban",
            settlements=(
                SalesSettlementIn(
                    target_kind=SettlementTargetKind.SALES_INVOICE,
                    target_id=entry.id,
                    amount_fc=goods + vat,
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        sales = SalesInvoiceService(session)
        before = sales.create(
            _sales_invoice(context, accounts, posting_date=AUG_10, due_date=SEP_20),
            user_id=ACTOR_ID,
        )
        sales.post(before.id, user_id=ACTOR_ID)
        ids["before"] = before.id
        inside = sales.create(
            _sales_invoice(context, accounts, posting_date=SEP_10, due_date=OCT_15),
            user_id=ACTOR_ID,
        )
        sales.post(inside.id, user_id=ACTOR_ID)
        ids["inside"] = inside.id
        ids["return_inside"] = _return_against(
            session,
            sales,
            context,
            accounts,
            before.id,
            goods=RETURN_GOODS,
            vat=RETURN_VAT,
            posting_date=SEP_05,
        )
        ids["return_after"] = _return_against(
            session,
            sales,
            context,
            accounts,
            before.id,
            goods=LATE_RETURN_GOODS,
            vat=LATE_RETURN_VAT,
            posting_date=OCT_05,
        )

        purchase = PurchaseInvoiceService(session)
        payable = purchase.create(
            PurchaseInvoiceIn(
                kind=PurchaseInvoiceKind.GOODS,
                operation_code="mua-hang-hoa",
                vendor_id=VENDOR_ID,
                payable_account_id=accounts["331"],
                branch_id=context.branch_id,
                document_date=SEP_10,
                posting_date=SEP_10,
                due_date=OCT_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                vendor_invoice_no="0007705",
                vendor_invoice_date=SEP_10,
                landed_cost_allocation=LandedCostAllocation.BY_VALUE,
                description="mua hàng — văn bản đối tác 7G-5",
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Hàng 7G-5",
                        item_id=ITEM_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=GOODS,
                        amount_fc=GOODS,
                        vat_rate=Decimal(10),
                        vat_amount_fc=VAT,
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        purchase.post(payable.id, user_id=ACTOR_ID)
        ids["payable"] = payable.id

    ids["opening"] = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER_ID,
        amount_fc=OPENING_AMOUNT,
        invoice_no="HD-DAU-KY-7G5",
        due_date=OPENING_DUE,
    )
    return ids


PARTNER_VIEW = permission_code("master", "partners", Action.VIEW)
SALES_VIEW = permission_code("sales", "invoice", Action.VIEW)
PURCHASE_VIEW = permission_code("purchase", "invoice", Action.VIEW)


@pytest.fixture(scope="module")
def roles(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> dict[str, str]:
    return {
        "full": ensure_role(
            session_factory, dataset_alpha, FULL_ROLE, [PARTNER_VIEW, SALES_VIEW, PURCHASE_VIEW]
        ),
        "sales_only": ensure_role(
            session_factory, dataset_alpha, SALES_ONLY_ROLE, [PARTNER_VIEW, SALES_VIEW]
        ),
        "partner_only": ensure_role(
            session_factory, dataset_alpha, PARTNER_ONLY_ROLE, [PARTNER_VIEW]
        ),
    }


Printer = Callable[..., "tuple[int, bytes]"]


@pytest.fixture
def printer(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    books: dict[str, UUID],
    roles: dict[str, str],
) -> Printer:
    del books
    cache: dict[str, dict[str, str]] = {}

    def run(role: str, path: str, params: dict[str, str] | None = None) -> tuple[int, bytes]:
        if role not in cache:
            cache[role] = {
                **actor(
                    client,
                    session_factory,
                    dataset_alpha,
                    user_factory,
                    roles[role],
                    f"in_van_ban_{role}",
                    test_password,
                    branch_codes=[context.branch_code],
                ),
                BRANCH_HEADER: str(context.branch_id),
            }
        response = client.post(path, params=params or {}, headers=cache[role])
        return response.status_code, response.content

    return run


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def _money(value: Decimal) -> str:
    return format_money(value, blank_zero=False)


def _statement_path(partner_id: int, kind: str) -> str:
    return f"/api/v1/partners/{partner_id}/documents/{kind}/print"


SEPTEMBER = {"from_date": SEP_01.isoformat(), "to_date": SEP_30.isoformat()}


class TestTheReceivableStatement:
    def test_the_four_period_numbers_come_from_the_right_places(self, printer: Printer) -> None:
        status, content = printer(
            "full", _statement_path(CUSTOMER_ID, "doi-chieu-phai-thu"), SEPTEMBER
        )
        assert status == 200, content[:200]
        text = _pdf_text(content)
        assert "BIÊN BẢN ĐỐI CHIẾU VÀ XÁC NHẬN CÔNG NỢ PHẢI THU" in text.upper()
        # Ghim TỪNG DÒNG của bảng kỳ (nhãn + số trên cùng dòng), vì hai con số
        # tăng/giảm trùng với ô "Giá trị"/"Đã TT" của bảng chi tiết — `in text`
        # trơn sẽ xanh cả khi bảng kỳ in sai (review 7G-5 M-4).
        for label, expected in (
            ("Số dư đầu kỳ", EXPECTED_OPENING),
            ("Phát sinh tăng trong kỳ", EXPECTED_INCREASE),
            ("Phát sinh giảm trong kỳ (đã thanh toán)", EXPECTED_DECREASE),
            ("Số dư cuối kỳ", EXPECTED_CLOSING),
        ):
            assert f"{label} {_money(expected)}" in text, f"thiếu dòng {label!r}:\n{text}"
        assert EXPECTED_OPENING + EXPECTED_INCREASE - EXPECTED_DECREASE == EXPECTED_CLOSING
        assert "Viết bằng chữ" in text
        # Nợ mang sang có mặt ở bảng chi tiết bằng số hóa đơn cũ.
        assert "HD-DAU-KY-7G5" in text.replace("\n", ""), text

    def test_a_settlement_posted_after_the_period_is_not_in_it(self, printer: Printer) -> None:
        """Trả lại tháng 10 không kéo về tháng 9 — nhưng kỳ tháng 10 thì thấy."""
        status, content = printer(
            "full",
            _statement_path(CUSTOMER_ID, "doi-chieu-phai-thu"),
            {"from_date": date(2026, 10, 1).isoformat(), "to_date": date(2026, 10, 31).isoformat()},
        )
        assert status == 200
        text = _pdf_text(content)
        late = LATE_RETURN_GOODS + LATE_RETURN_VAT
        assert f"Số dư đầu kỳ {_money(EXPECTED_CLOSING)}" in text, "đầu kỳ 10 = cuối kỳ 9"
        assert f"Phát sinh giảm trong kỳ (đã thanh toán) {_money(late)}" in text
        assert f"Số dư cuối kỳ {_money(EXPECTED_CLOSING - late)}" in text

    def test_the_payable_side_does_not_leak_onto_the_receivable_statement(
        self, printer: Printer
    ) -> None:
        status, content = printer(
            "full", _statement_path(VENDOR_ID, "doi-chieu-phai-thu"), SEPTEMBER
        )
        assert status == 200
        text = _pdf_text(content)
        assert "0007705" not in text
        assert f"Số dư cuối kỳ {_money(Decimal(0))}" in text

    def test_reversed_dates_are_rejected(self, printer: Printer) -> None:
        status, _ = printer(
            "full",
            _statement_path(CUSTOMER_ID, "doi-chieu-phai-thu"),
            {"from_date": SEP_30.isoformat(), "to_date": SEP_01.isoformat()},
        )
        assert status == 422


class TestThePayableStatement:
    def test_the_vendor_statement_lists_the_purchase_invoice(self, printer: Printer) -> None:
        status, content = printer(
            "full", _statement_path(VENDOR_ID, "doi-chieu-phai-tra"), SEPTEMBER
        )
        assert status == 200, content[:200]
        text = _pdf_text(content)
        assert "BIÊN BẢN ĐỐI CHIẾU VÀ XÁC NHẬN CÔNG NỢ PHẢI TRẢ" in text.upper()
        assert _money(TOTAL) in text
        assert "Bên A (Bên mua)" in text

    def test_the_sales_permission_alone_cannot_print_it(self, printer: Printer) -> None:
        status, _ = printer(
            "sales_only", _statement_path(VENDOR_ID, "doi-chieu-phai-tra"), SEPTEMBER
        )
        assert status == 403


class TestTheDebtNotice:
    def test_it_states_open_overdue_and_where_to_pay(self, printer: Printer) -> None:
        status, content = printer(
            "full",
            f"/api/v1/partners/{CUSTOMER_ID}/documents/thong-bao-cong-no/print",
            {"as_of": SEP_30.isoformat()},
        )
        assert status == 200, content[:200]
        text = _pdf_text(content)
        assert "THÔNG BÁO CÔNG NỢ" in text.upper()
        # Còn nợ tại 30/09 = cuối kỳ 9; quá hạn = hóa đơn A còn lại (hạn 20/09) + nợ mang sang.
        assert _money(EXPECTED_CLOSING) in text
        overdue = TOTAL - RETURN_TOTAL + OPENING_AMOUNT
        assert _money(overdue) in text
        assert BANK_ACCOUNT_CODE in text
        assert "Kính gửi" in text

    def test_an_unknown_partner_is_404(self, printer: Printer) -> None:
        status, _ = printer(
            "full", "/api/v1/partners/9499999/documents/thong-bao-cong-no/print", {}
        )
        assert status == 404
