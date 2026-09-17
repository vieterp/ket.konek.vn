"""BFF thẻ công nợ đối tác (lát 7G-4, nợ H56) — `GET /api/v1/partners/{id}/overview`.

Ba điều thẻ phải nói đúng: (1) số còn nợ và số quá hạn của MỖI chiều là con số
của báo cáo tuổi nợ — cùng dataset, nên hóa đơn có hạn, hóa đơn chưa đến hạn và
nợ mang sang từ số dư ban đầu cộng đúng như tờ giấy in ra; (2) mỗi nửa chỉ hiện
với người có quyền xem chứng từ của chiều đó — thiếu quyền là `null`, không phải
403, và không phải một con số 0 giả; (3) `credit_available` chỉ có nghĩa khi vừa
có ngưỡng vừa thấy được nửa phải thu.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_open_invoice, seed_opening_advance
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.purchase.models import LandedCostAllocation, PurchaseInvoiceKind
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.opening_balances.models import OpeningDetailKind
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit, ensure_vendor, seed_purchase_package_data
from report_preview_support import PreviewResult
from sales_support import ensure_customer, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
FULL_ROLE = "xem_the_cong_no_du_quyen"
SALES_ONLY_ROLE = "xem_the_cong_no_chi_ban"
PARTNER_ONLY_ROLE = "xem_the_cong_no_chi_danh_muc"
NO_PARTNER_ROLE = "xem_the_cong_no_khong_danh_muc"

SEP_05 = date(2026, 9, 5)
SEP_10 = date(2026, 9, 10)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)
OCT_15 = date(2026, 10, 15)
OPENING_DUE = date(2026, 2, 20)

# Khối id 945x–948x: `dataset_alpha` dùng chung cả phiên, và khối 97xx (bản đầu
# của lát) đã thuộc họ test hóa đơn điện tử — helper `ensure_*` chạy trước thắng,
# bản ghi mang tên khác, và bài chỉ đỏ khi chạy CẢ nhóm `db` (bẫy thứ tự tệp).
CUSTOMER_ID = 9455
VENDOR_ID = 9456
UNIT_ID = 9465
ITEM_ID = 9475
CUSTOMER_CODE = "KH-7G4-THE"
VENDOR_CODE = "NCC-7G4-THE"

GOODS = Decimal(1_000_000)
VAT = Decimal(100_000)
TOTAL = GOODS + VAT
OPENING_AMOUNT = Decimal(500_000)
CUSTOMER_DEPOSIT = Decimal(300_000)  # khách ứng trước đầu kỳ → phía PHẢI TRẢ của khách
VENDOR_PREPAYMENT = Decimal(200_000)  # ta trả trước đầu kỳ cho chính đối tác ấy → phía PHẢI THU
CREDIT_LIMIT = Decimal(5_000_000)

RECEIVABLE_DEBT = TOTAL + TOTAL + OPENING_AMOUNT  # quá hạn + chưa đến hạn + mang sang
RECEIVABLE_OPEN = RECEIVABLE_DEBT + VENDOR_PREPAYMENT  # thẻ hiện CẢ khoản ứng trước…
RECEIVABLE_OVERDUE = TOTAL + OPENING_AMOUNT
PAYABLE_OPEN = GOODS  # hóa đơn mua NOT_YET không mang thuế (BR-PUR-02)


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
        ensure_customer(
            session, partner_id=CUSTOMER_ID, code=CUSTOMER_CODE, credit_limit=CREDIT_LIMIT
        )
        ensure_vendor(session, partner_id=VENDOR_ID, code=VENDOR_CODE)
        ensure_unit(session, unit_id=UNIT_ID, code="Cai-7G4-THE")
        ensure_item(session, item_id=ITEM_ID, code="VT-7G4-THE", unit_id=UNIT_ID)
    return codes


def _sales_invoice(
    context: PostingContext, accounts: dict[str, int], *, posting_date: date, due_date: date
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="thẻ công nợ 7G-4",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng 7G-4",
                item_id=ITEM_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(1),
                unit_price_fc=GOODS,
                amount_fc=GOODS,
                vat_rate=Decimal(10),
                vat_amount_fc=VAT,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
            ),
        ),
    )


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
        for key, posting_date, due in (("overdue", SEP_05, SEP_20), ("future", SEP_10, OCT_15)):
            invoice = sales.create(
                _sales_invoice(context, accounts, posting_date=posting_date, due_date=due),
                user_id=ACTOR_ID,
            )
            sales.post(invoice.id, user_id=ACTOR_ID)
            ids[key] = invoice.id

        purchase = PurchaseInvoiceService(session)
        payable = purchase.create(
            PurchaseInvoiceIn(
                kind=PurchaseInvoiceKind.GOODS,
                operation_code="mua-hang-hoa",
                vendor_id=VENDOR_ID,
                payable_account_id=accounts["331"],
                branch_id=context.branch_id,
                document_date=SEP_05,
                posting_date=SEP_05,
                due_date=SEP_20,
                currency_code="VND",
                exchange_rate=Decimal(1),
                vendor_invoice_status=1,
                landed_cost_allocation=LandedCostAllocation.BY_VALUE,
                description="mua hàng — thẻ công nợ 7G-4",
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Hàng 7G-4",
                        item_id=ITEM_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=GOODS,
                        amount_fc=GOODS,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["156"],
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
        invoice_no="HD-DAU-KY-7G4-THE",
        due_date=OPENING_DUE,
    )
    # Hai khoản ứng trước đầu kỳ trên CÙNG đối tác — dòng không có số/ngày hóa
    # đơn (`advance_has_no_invoice_ref`), là ca làm bản đầu của lát đổ 500
    # (review 7G-4 C-1). Khách ứng → phía phải trả; ta trả trước (đối tác này
    # đồng thời là người bán) → phía phải thu, và KHÔNG được trừ vào ngưỡng nợ.
    ids["deposit"] = seed_opening_advance(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.RECEIVABLE,
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER_ID,
        amount=CUSTOMER_DEPOSIT,
    )
    ids["prepayment"] = seed_opening_advance(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=CUSTOMER_ID,
        amount=VENDOR_PREPAYMENT,
    )
    return ids


PARTNER_VIEW = permission_code("master", "partners", Action.VIEW)
SALES_VIEW = permission_code("sales", "invoice", Action.VIEW)
PURCHASE_VIEW = permission_code("purchase", "invoice", Action.VIEW)


@pytest.fixture(scope="module")
def roles(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> dict[str, str]:
    return {
        "full": ensure_role(
            session_factory,
            dataset_alpha,
            FULL_ROLE,
            [PARTNER_VIEW, SALES_VIEW, PURCHASE_VIEW, "reporting.report.view"],
        ),
        "sales_only": ensure_role(
            session_factory, dataset_alpha, SALES_ONLY_ROLE, [PARTNER_VIEW, SALES_VIEW]
        ),
        "partner_only": ensure_role(
            session_factory, dataset_alpha, PARTNER_ONLY_ROLE, [PARTNER_VIEW]
        ),
        "no_partner": ensure_role(
            session_factory, dataset_alpha, NO_PARTNER_ROLE, [SALES_VIEW, PURCHASE_VIEW]
        ),
    }


class OverviewClient:
    """Gọi BFF dưới một vai trò; `headers(role)` để bài kiểm gọi thêm cửa khác
    (báo cáo) với CÙNG người dùng."""

    def __init__(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        roles: dict[str, str],
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._dataset = dataset_alpha
        self._user_factory = user_factory
        self._password = test_password
        self._context = context
        self._roles = roles
        self._headers: dict[str, dict[str, str]] = {}

    def headers(self, role: str) -> dict[str, str]:
        if role not in self._headers:
            self._headers[role] = {
                **actor(
                    self._client,
                    self._session_factory,
                    self._dataset,
                    self._user_factory,
                    self._roles[role],
                    f"the_cong_no_{role}",
                    self._password,
                    branch_codes=[self._context.branch_code],
                ),
                BRANCH_HEADER: str(self._context.branch_id),
            }
        return self._headers[role]

    def __call__(self, role: str, partner_id: int = CUSTOMER_ID) -> tuple[int, dict[str, object]]:
        response = self._client.get(
            f"/api/v1/partners/{partner_id}/overview",
            params={"as_of": SEP_30.isoformat()},
            headers=self.headers(role),
        )
        body = response.json()
        assert isinstance(body, dict)
        return response.status_code, body


@pytest.fixture
def overview(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    books: dict[str, UUID],
    roles: dict[str, str],
) -> OverviewClient:
    del books  # chỉ để bộ gieo chạy trước
    return OverviewClient(
        client, session_factory, dataset_alpha, user_factory, test_password, context, roles
    )


class TestTheCard:
    def test_both_halves_are_the_aging_report_numbers(self, overview: OverviewClient) -> None:
        status, body = overview("full")
        assert status == 200, body
        partner = body["partner"]
        assert partner["code"] == CUSTOMER_CODE
        assert isinstance(partner["uid"], str)
        assert Decimal(partner["credit_limit"]) == CREDIT_LIMIT

        debt = body["debt"]
        assert debt["as_of"] == SEP_30.isoformat()
        receivable = debt["receivable"]
        assert Decimal(receivable["open_amount"]) == RECEIVABLE_OPEN
        assert receivable["open_count"] == 4
        assert Decimal(receivable["overdue_amount"]) == RECEIVABLE_OVERDUE
        assert receivable["overdue_count"] == 2
        assert receivable["oldest_due_date"] == OPENING_DUE.isoformat()
        # Phía phải trả của khách chỉ có khoản khách ứng trước đầu kỳ — một dòng
        # không số, không ngày, không hạn.
        payable = debt["payable"]
        assert Decimal(payable["open_amount"]) == CUSTOMER_DEPOSIT
        assert payable["open_count"] == 1
        assert payable["oldest_due_date"] is None

        assert Decimal(debt["credit_limit"]) == CREDIT_LIMIT
        # Ngưỡng trừ phần nợ THẬT, không trừ khoản ta trả trước — cùng luật guard.
        assert Decimal(debt["credit_available"]) == CREDIT_LIMIT - RECEIVABLE_DEBT

    def test_the_receivable_half_is_what_the_aging_report_prints(
        self, overview: OverviewClient, client: TestClient
    ) -> None:
        """Thẻ và tờ `chi-tiet-tuoi-no-phai-thu` phải là MỘT con số: cùng
        dataset, cùng mốc, cùng đối tác — nếu ai đó ghim lại `partner_kind` ở
        một trong hai cửa, bài này đỏ trước người dùng."""
        status, body = overview("full")
        assert status == 200, body
        headers = overview.headers("full")
        response = client.post(
            "/api/v1/reports/chi-tiet-tuoi-no-phai-thu/preview",
            json={
                "params": {
                    "from_date": SEP_05.isoformat(),
                    "to_date": SEP_30.isoformat(),
                    "partner_id": CUSTOMER_ID,
                }
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        sheet = PreviewResult(response.json())
        assert sheet.sum_of("remaining") == Decimal(body["debt"]["receivable"]["open_amount"])
        assert len(sheet.rows) == body["debt"]["receivable"]["open_count"]

    def test_the_vendor_card_reads_the_payable_side(self, overview: OverviewClient) -> None:
        status, body = overview("full", VENDOR_ID)
        assert status == 200, body
        debt = body["debt"]
        assert Decimal(debt["payable"]["open_amount"]) == PAYABLE_OPEN
        assert debt["payable"]["overdue_count"] == 1
        assert debt["payable"]["oldest_due_date"] == SEP_20.isoformat()
        assert debt["receivable"]["open_count"] == 0
        # Không khai ngưỡng → không có "còn được nợ".
        assert debt["credit_limit"] is None
        assert debt["credit_available"] is None

    def test_the_half_you_may_not_see_is_null_not_zero(self, overview: OverviewClient) -> None:
        status, body = overview("sales_only")
        assert status == 200, body
        debt = body["debt"]
        assert debt["payable"] is None
        assert Decimal(debt["receivable"]["open_amount"]) == RECEIVABLE_OPEN
        assert Decimal(debt["credit_available"]) == CREDIT_LIMIT - RECEIVABLE_DEBT

    def test_the_partner_permission_alone_shows_the_profile_and_no_debt(
        self, overview: OverviewClient
    ) -> None:
        status, body = overview("partner_only")
        assert status == 200, body
        assert body["partner"]["code"] == CUSTOMER_CODE
        debt = body["debt"]
        assert debt["receivable"] is None
        assert debt["payable"] is None
        assert Decimal(debt["credit_limit"]) == CREDIT_LIMIT
        # Không thấy nửa phải thu thì không có gì để trừ khỏi ngưỡng.
        assert debt["credit_available"] is None

    def test_without_the_partner_permission_it_is_403(self, overview: OverviewClient) -> None:
        status, _ = overview("no_partner")
        assert status == 403

    def test_an_unknown_partner_is_404(self, overview: OverviewClient) -> None:
        status, _ = overview("full", 9_799_999)
        assert status == 404
