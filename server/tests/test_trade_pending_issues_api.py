"""BFF tab "việc còn thiếu" mua/bán (lát 7G-4, U1) — `GET /api/v1/{purchase,sales}/pending-issues`.

Ba nhóm mỗi chiều, và bài kiểm đi theo ba câu hỏi mà tab phải trả lời đúng:

* "chưa ghi sổ" là chứng từ Đã cất — không phải chứng từ đã ghi sổ mà thiếu gì đó;
* "chưa có hóa đơn" ở chiều bán đếm theo tờ HĐĐT **còn sống** (quyết định user
  2026-09-17): tờ nháp, tờ bị từ chối, tờ đã hủy đều KHÔNG cứu được chứng từ khỏi
  nhóm; ở chiều mua đếm `vendor_invoice_status = NOT_YET`, còn `NONE` (mua của
  cá nhân) là "không có" chứ không "còn thiếu";
* "quá hạn" là con số của báo cáo tuổi nợ, không phải một phép cộng riêng: phần
  còn nợ của mỗi dòng phải bằng `remaining` mà `chi-tiet-tuoi-no-*` in ra, và
  nợ mang sang từ số dư ban đầu vẫn là một việc dù không có chứng từ để mở.

Mỗi tệp test có chi nhánh riêng (`seed_posting_context`), nên người dùng ở
đây chỉ thấy đúng những chứng từ tệp này gieo — không bài nào phải trừ đi
"phần của tệp khác".
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

from cash_book_support import seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice.error_flow import ErrorFlowService
from ket.modules.einvoice.models import ErrorKind, ErrorNoticeKind
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.purchase.models import (
    LandedCostAllocation,
    PurchaseInvoiceKind,
    VendorInvoiceStatus,
)
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit, ensure_vendor, seed_purchase_package_data
from sales_support import ensure_customer, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
SALES_ROLE = "xem_viec_con_thieu_ban_hang"
PURCHASE_ROLE = "xem_viec_con_thieu_mua_hang"
NEITHER_ROLE = "xem_viec_con_thieu_khong_quyen"

AUG_01 = date(2026, 8, 1)
AUG_10 = date(2026, 8, 10)
SEP_05 = date(2026, 9, 5)
SEP_10 = date(2026, 9, 10)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)  # mốc `as_of` của mọi bài
OCT_15 = date(2026, 10, 15)
OPENING_DUE = date(2026, 2, 20)

# Khối id 945x–948x: `dataset_alpha` dùng chung cả phiên, và khối 97xx (bản đầu
# của lát) đã thuộc họ test hóa đơn điện tử — helper `ensure_*` chạy trước thắng,
# bản ghi mang tên khác, và bài chỉ đỏ khi chạy CẢ nhóm `db` (bẫy thứ tự tệp).
CUSTOMER_ID = 9451
VENDOR_ID = 9452
UNIT_ID = 9461
ITEM_ID = 9471
FORM_ID = 9481
SERIAL = "C26TGE"

CUSTOMER_CODE = "KH-7G4-01"
VENDOR_CODE = "NCC-7G4-01"

GOODS = Decimal(1_000_000)
VAT = Decimal(100_000)
TOTAL = GOODS + VAT
OPENING_AMOUNT = Decimal(500_000)


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
        ensure_unit(session, unit_id=UNIT_ID, code="Cai-7G4")
        ensure_item(session, item_id=ITEM_ID, code="VT-7G4", unit_id=UNIT_ID)
        ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
        ensure_active_registration(
            session, branch_id=context.branch_id, invoice_form_id=FORM_ID, start_date=AUG_01
        )
    return codes


def _sales_invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    due_date: date | None,
    kind: int = SalesInvoiceKind.GOODS,
    operation: str = "ban-hang-hoa",
    description: str,
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
        description=description,
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


def _purchase_invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    due_date: date | None,
    vendor_invoice_status: int,
    description: str,
) -> PurchaseInvoiceIn:
    received = vendor_invoice_status == VendorInvoiceStatus.RECEIVED
    return PurchaseInvoiceIn(
        kind=PurchaseInvoiceKind.GOODS,
        operation_code="mua-hang-hoa",
        vendor_id=VENDOR_ID,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        vendor_invoice_status=vendor_invoice_status,
        vendor_invoice_no="0000741" if received else None,
        vendor_invoice_date=posting_date if received else None,
        landed_cost_allocation=LandedCostAllocation.BY_VALUE,
        description=description,
        lines=(
            PurchaseInvoiceLineIn(
                description="Hàng 7G-4",
                item_id=ITEM_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(1),
                unit_price_fc=GOODS,
                amount_fc=GOODS,
                # BR-PUR-02: chưa có / không có hóa đơn thì không được khấu trừ
                # thuế đầu vào — thân chứng từ ấy không mang dòng thuế.
                vat_rate=Decimal(10) if received else Decimal(0),
                vat_amount_fc=VAT if received else Decimal(0),
                account_id=accounts["156"],
                vat_account_id=accounts["1331"] if received else None,
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
    """Bộ chứng từ phủ đủ ba nhóm ở cả hai chiều, kèm bốn tình trạng HĐĐT."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        sales = SalesInvoiceService(session)
        einvoices = EInvoiceService(session)

        # Chiều BÁN.
        draft = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="nháp"
            ),
            user_id=ACTOR_ID,
        )
        ids["sales_draft"] = draft.id

        # Ghi sổ, KHÔNG có tờ HĐĐT, hạn 20/09 → vừa "chưa có hóa đơn" vừa "quá hạn".
        bare = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_05, due_date=SEP_20, description="không tờ"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(bare.id, user_id=ACTOR_ID)
        ids["sales_bare"] = bare.id

        # Ghi sổ, tờ ĐÃ PHÁT HÀNH có mã CQT, hạn 15/10 → không thuộc nhóm nào.
        issued = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="đã cấp mã"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(issued.id, user_id=ACTOR_ID)
        issued_sheet = einvoices.create_draft(source_voucher_id=issued.id, invoice_form_id=FORM_ID)
        einvoices.issue(issued_sheet.id, invoice_date=SEP_10)
        einvoices.confirm(issued_sheet.id, tax_authority_code="M1-7G4-0001")
        ids["sales_issued"] = issued.id

        # Ghi sổ, tờ chỉ mới NHÁP → vẫn "chưa có hóa đơn".
        drafted = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="tờ nháp"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(drafted.id, user_id=ACTOR_ID)
        einvoices.create_draft(source_voucher_id=drafted.id, invoice_form_id=FORM_ID)
        ids["sales_sheet_draft"] = drafted.id

        # Ghi sổ, tờ bị CQT TỪ CHỐI → vẫn "chưa có hóa đơn".
        rejected = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="tờ lỗi"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(rejected.id, user_id=ACTOR_ID)
        rejected_sheet = einvoices.create_draft(
            source_voucher_id=rejected.id, invoice_form_id=FORM_ID
        )
        einvoices.issue(rejected_sheet.id, invoice_date=SEP_10)
        einvoices.reject(rejected_sheet.id, message="cơ quan thuế từ chối — bài kiểm 7G-4")
        ids["sales_sheet_rejected"] = rejected.id

        # Ghi sổ, tờ ĐÃ HỦY (đủ hai văn bản, BR-EIV-04) → vẫn "chưa có hóa đơn".
        cancelled = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="tờ hủy"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(cancelled.id, user_id=ACTOR_ID)
        cancelled_sheet = einvoices.create_draft(
            source_voucher_id=cancelled.id, invoice_form_id=FORM_ID
        )
        einvoices.issue(cancelled_sheet.id, invoice_date=SEP_10)
        einvoices.confirm(cancelled_sheet.id, tax_authority_code="M1-7G4-0002")
        for notice_kind, notice_no in (
            (ErrorNoticeKind.THONG_BAO_HUY, "TBH-7G4-01"),
            (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-7G4-01"),
        ):
            notice = einvoices.add_notice(
                cancelled_sheet.id,
                kind=notice_kind,
                notice_no=notice_no,
                notice_date=SEP_10,
                reason="hủy theo thỏa thuận — bài kiểm 7G-4",
            )
            einvoices.submit_notice(notice.id)
        einvoices.cancel(cancelled_sheet.id)
        ids["sales_sheet_cancelled"] = cancelled.id

        # Ghi sổ, tờ đã phát hành rồi bị THAY THẾ; tờ thay thế còn NHÁP và treo
        # lên CHÍNH chứng từ này (review 7G-4 H-1) → vẫn "chưa có hóa đơn". Khi
        # tờ mới phát hành thì chứng từ tự rời nhóm — bài dưới kiểm cả hai nửa.
        replaced = sales.create(
            _sales_invoice(
                context, accounts, posting_date=SEP_10, due_date=OCT_15, description="tờ thay"
            ),
            user_id=ACTOR_ID,
        )
        sales.post(replaced.id, user_id=ACTOR_ID)
        replaced_sheet = einvoices.create_draft(
            source_voucher_id=replaced.id, invoice_form_id=FORM_ID
        )
        einvoices.issue(replaced_sheet.id, invoice_date=SEP_10)
        einvoices.confirm(replaced_sheet.id, tax_authority_code="M1-7G4-0003")
        outcome = ErrorFlowService(session).apply(
            replaced_sheet.id,
            error_kind=ErrorKind.SAI_SO_TIEN,
            buyer_declared=False,
            notice_no="TBSS-7G4-01",
            notice_date=SEP_10,
        )
        assert outcome.replacement is not None
        ids["sales_replaced"] = replaced.id
        ids["replacement_sheet"] = outcome.replacement.id

        # Chiều MUA.
        purchase = PurchaseInvoiceService(session)
        purchase_draft = purchase.create(
            _purchase_invoice(
                context,
                accounts,
                posting_date=SEP_10,
                due_date=OCT_15,
                vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
                description="nháp mua",
            ),
            user_id=ACTOR_ID,
        )
        ids["purchase_draft"] = purchase_draft.id

        # Hàng về trước, hóa đơn về sau; hạn 20/09 → "chưa có hóa đơn" + "quá hạn".
        not_yet = purchase.create(
            _purchase_invoice(
                context,
                accounts,
                posting_date=SEP_05,
                due_date=SEP_20,
                vendor_invoice_status=VendorInvoiceStatus.NOT_YET,
                description="hóa đơn về sau",
            ),
            user_id=ACTOR_ID,
        )
        purchase.post(not_yet.id, user_id=ACTOR_ID)
        ids["purchase_not_yet"] = not_yet.id

        # Đã nhận hóa đơn, hạn 15/10 → không thuộc nhóm nào.
        received = purchase.create(
            _purchase_invoice(
                context,
                accounts,
                posting_date=SEP_10,
                due_date=OCT_15,
                vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
                description="đã nhận hóa đơn",
            ),
            user_id=ACTOR_ID,
        )
        purchase.post(received.id, user_id=ACTOR_ID)
        ids["purchase_received"] = received.id

        # Mua của cá nhân — KHÔNG có hóa đơn, và đó không phải việc còn thiếu.
        none = purchase.create(
            _purchase_invoice(
                context,
                accounts,
                posting_date=SEP_10,
                due_date=None,
                vendor_invoice_status=VendorInvoiceStatus.NONE,
                description="mua của cá nhân",
            ),
            user_id=ACTOR_ID,
        )
        purchase.post(none.id, user_id=ACTOR_ID)
        ids["purchase_none"] = none.id

    # Nợ phải thu mang sang từ hệ thống cũ, hạn 20/02 — không có chứng từ để mở.
    ids["opening_receivable"] = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER_ID,
        amount_fc=OPENING_AMOUNT,
        invoice_no="HD-DAU-KY-7G4",
        due_date=OPENING_DUE,
    )
    return ids


def _headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    role: str,
    prefix: str,
) -> dict[str, str]:
    return {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            role,
            prefix,
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }


@pytest.fixture(scope="module")
def sales_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        SALES_ROLE,
        [permission_code("sales", "invoice", Action.VIEW)],
    )


@pytest.fixture(scope="module")
def purchase_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        PURCHASE_ROLE,
        [permission_code("purchase", "invoice", Action.VIEW)],
    )


@pytest.fixture(scope="module")
def neither_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        NEITHER_ROLE,
        [permission_code("master", "partners", Action.VIEW)],
    )


def _groups(body: dict[str, object]) -> dict[str, dict[str, object]]:
    groups = body["groups"]
    assert isinstance(groups, list)
    return {str(group["code"]): group for group in groups}


def _sample_nos(group: dict[str, object]) -> list[str]:
    sample = group["sample"]
    assert isinstance(sample, list)
    return [str(row["voucher_no"]) for row in sample]


class TestTheSalesTab:
    @pytest.fixture
    def body(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        books: dict[str, UUID],
        sales_role: str,
    ) -> dict[str, object]:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            sales_role,
            "viec_con_thieu_ban",
        )
        response = client.get(
            "/api/v1/sales/pending-issues",
            params={"as_of": SEP_30.isoformat()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert isinstance(payload, dict)
        return payload

    def test_the_tab_names_its_side_and_its_date(self, body: dict[str, object]) -> None:
        assert body["side"] == "sales"
        assert body["as_of"] == SEP_30.isoformat()

    def test_unposted_counts_only_saved_vouchers(
        self, body: dict[str, object], books: dict[str, UUID]
    ) -> None:
        group = _groups(body)["chua-ghi-so"]
        assert group["count"] == 1
        assert group["next_action"] == "post"
        (row,) = group["sample"]
        assert row["voucher_id"] == str(books["sales_draft"])
        assert row["partner_code"] == CUSTOMER_CODE
        assert Decimal(row["amount_fc"]) == TOTAL
        # Chưa đến hạn → 0, không phải rỗng: "không có hạn" là một tình trạng khác.
        assert row["days_overdue"] == 0

    def test_missing_einvoice_counts_dead_sheets_as_absent(
        self, body: dict[str, object], books: dict[str, UUID]
    ) -> None:
        """Tờ nháp, tờ bị từ chối, tờ đã hủy — không tờ nào là "đã có hóa đơn"."""
        group = _groups(body)["chua-co-hoa-don"]
        assert group["next_action"] == "issue-einvoice"
        assert group["count"] == 5
        ids = {row["voucher_id"] for row in group["sample"]}
        assert ids == {
            str(books["sales_bare"]),
            str(books["sales_sheet_draft"]),
            str(books["sales_sheet_rejected"]),
            str(books["sales_sheet_cancelled"]),
            str(books["sales_replaced"]),
        }
        assert str(books["sales_issued"]) not in ids
        assert str(books["sales_draft"]) not in ids, "chứng từ chưa ghi sổ thuộc nhóm khác"

    def test_overdue_is_the_aging_report_number_and_keeps_carried_debt(
        self, body: dict[str, object], books: dict[str, UUID]
    ) -> None:
        group = _groups(body)["qua-han"]
        assert group["next_action"] == "collect"
        assert group["count"] == 2
        by_no = {row["voucher_no"]: row for row in group["sample"]}
        # Sắp theo hạn sớm nhất: nợ mang sang (20/02) đứng trước hóa đơn (20/09).
        assert _sample_nos(group) == ["HD-DAU-KY-7G4", *[n for n in by_no if n != "HD-DAU-KY-7G4"]]
        carried = by_no["HD-DAU-KY-7G4"]
        assert carried["voucher_id"] is None
        assert carried["source_label"] == "Số dư đầu kỳ"
        assert Decimal(carried["amount_fc"]) == OPENING_AMOUNT
        assert carried["days_overdue"] == (SEP_30 - OPENING_DUE).days
        (invoice,) = [row for row in group["sample"] if row["voucher_id"] is not None]
        assert invoice["voucher_id"] == str(books["sales_bare"])
        assert invoice["source_label"] == "Hóa đơn bán"
        assert Decimal(invoice["amount_fc"]) == TOTAL
        assert invoice["days_overdue"] == (SEP_30 - SEP_20).days

    def test_issuing_the_replacement_sheet_clears_the_voucher(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        books: dict[str, UUID],
        sales_role: str,
    ) -> None:
        """Tờ cũ `DA_THAY_THE` không cứu chứng từ; tờ MỚI phát hành mới cứu —
        và bài này chạy SAU bài đếm 5 ở trên (thứ tự trong lớp), nên nó là bài
        cuối chạm bộ gieo dùng chung của module."""
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            EInvoiceService(session).issue(books["replacement_sheet"], invoice_date=SEP_10)
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            sales_role,
            "viec_con_thieu_ban_thay",
        )
        response = client.get(
            "/api/v1/sales/pending-issues",
            params={"as_of": SEP_30.isoformat()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        group = _groups(response.json())["chua-co-hoa-don"]
        assert group["count"] == 4
        assert str(books["sales_replaced"]) not in {row["voucher_id"] for row in group["sample"]}

    def test_nothing_overdue_before_the_due_dates(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        books: dict[str, UUID],
        sales_role: str,
    ) -> None:
        """Nhóm không có việc thì VẮNG MẶT, không phải `count: 0`."""
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            sales_role,
            "viec_con_thieu_ban_som",
        )
        response = client.get(
            "/api/v1/sales/pending-issues",
            params={"as_of": date(2026, 1, 31).isoformat()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert "qua-han" not in _groups(response.json())


class TestThePurchaseTab:
    @pytest.fixture
    def body(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        books: dict[str, UUID],
        purchase_role: str,
    ) -> dict[str, object]:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            purchase_role,
            "viec_con_thieu_mua",
        )
        response = client.get(
            "/api/v1/purchase/pending-issues",
            params={"as_of": SEP_30.isoformat()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert isinstance(payload, dict)
        return payload

    def test_unposted(self, body: dict[str, object], books: dict[str, UUID]) -> None:
        group = _groups(body)["chua-ghi-so"]
        assert group["count"] == 1
        (row,) = group["sample"]
        assert row["voucher_id"] == str(books["purchase_draft"])
        assert row["partner_code"] == VENDOR_CODE

    def test_missing_vendor_invoice_is_not_yet_only(
        self, body: dict[str, object], books: dict[str, UUID]
    ) -> None:
        """`NONE` (mua của cá nhân) là "không có", không phải "còn thiếu"."""
        group = _groups(body)["chua-co-hoa-don"]
        assert group["next_action"] == "attach-vendor-invoice"
        assert group["count"] == 1
        (row,) = group["sample"]
        assert row["voucher_id"] == str(books["purchase_not_yet"])

    def test_overdue_payables(self, body: dict[str, object], books: dict[str, UUID]) -> None:
        group = _groups(body)["qua-han"]
        assert group["next_action"] == "pay"
        assert group["count"] == 1
        (row,) = group["sample"]
        assert row["voucher_id"] == str(books["purchase_not_yet"])
        assert Decimal(row["amount_fc"]) == GOODS, "không có thuế khi chưa có hóa đơn (BR-PUR-02)"
        assert row["days_overdue"] == (SEP_30 - SEP_20).days

    def test_the_undated_payable_is_not_overdue(
        self, body: dict[str, object], books: dict[str, UUID]
    ) -> None:
        overdue_ids = {row["voucher_id"] for row in _groups(body)["qua-han"]["sample"]}
        assert str(books["purchase_none"]) not in overdue_ids


class TestPermissions:
    @pytest.mark.parametrize(
        "path", ["/api/v1/sales/pending-issues", "/api/v1/purchase/pending-issues"]
    )
    def test_without_the_module_view_permission_it_is_403(
        self,
        path: str,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        neither_role: str,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            neither_role,
            "viec_con_thieu_khong_quyen",
        )
        response = client.get(path, headers=headers)
        assert response.status_code == 403, response.text

    def test_the_sales_permission_does_not_open_the_purchase_tab(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        sales_role: str,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            test_password,
            context,
            sales_role,
            "viec_con_thieu_ban_sang_mua",
        )
        response = client.get("/api/v1/purchase/pending-issues", headers=headers)
        assert response.status_code == 403, response.text
