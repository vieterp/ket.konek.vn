"""Lưới chứng từ bán hàng (lát 7H-2a, bộ xương màn 01 dùng lại) — `GET /api/v1/sales/invoices`.

Lưới phải nói được ba điều mà `/vouchers` không nói: khách hàng là ai, hóa đơn
đã có chưa (tờ HĐĐT **còn sống** — cùng định nghĩa nhóm `chua-co-hoa-don` của
BFF `pending-issues`), và **còn phải thu bao nhiêu hiện nay** — con số cuối đọc
từ dòng sổ phụ của chính chứng từ nên một lượt đối trừ (trả lại hàng) phải làm
nó nhích đúng số đối trừ. Dòng tổng tính trên toàn tập lọc, ba loại giảm trừ
mang dấu âm (doanh thu ròng).

Khối id 997x: `dataset_alpha` dùng chung cả phiên — helper `ensure_*` chạy trước
thắng, nên tệp này không dùng lại số của tệp khác.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice.error_flow import ErrorFlowService
from ket.modules.einvoice.models import ErrorKind
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit
from sales_support import ensure_customer, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
READER_ROLE = "xem_luoi_ban_hang_7h2a"
OTHER_ROLE = "khong_quyen_ban_hang_7h2a"

AUG_01 = date(2026, 8, 1)
SEP_05 = date(2026, 9, 5)
SEP_10 = date(2026, 9, 10)
SEP_12 = date(2026, 9, 12)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)
OCT_15 = date(2026, 10, 15)

CUSTOMER_A = 9971
CUSTOMER_B = 9972
CUSTOMER_A_CODE = "KH-7H2A-A"
CUSTOMER_B_CODE = "KH-7H2A-B"
UNIT_ID = 9973
ITEM_ID = 9974
FORM_ID = 9975
SERIAL = "C26THB"

GOODS = Decimal(1_000_000)
VAT = Decimal(100_000)
TOTAL = GOODS + VAT
RETURNED = Decimal(330_000)


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
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_A, code=CUSTOMER_A_CODE)
        ensure_customer(session, partner_id=CUSTOMER_B, code=CUSTOMER_B_CODE)
        ensure_unit(session, unit_id=UNIT_ID, code="Cai-7H2A")
        ensure_item(session, item_id=ITEM_ID, code="VT-7H2A", unit_id=UNIT_ID)
        ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
        ensure_active_registration(
            session, branch_id=context.branch_id, invoice_form_id=FORM_ID, start_date=AUG_01
        )
    return codes


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    customer_id: int,
    posting_date: date,
    due_date: date | None,
    kind: int = SalesInvoiceKind.GOODS,
    operation: str = "ban-hang-hoa",
    amount: Decimal = GOODS,
    vat: Decimal = VAT,
    settlements: tuple[SalesSettlementIn, ...] = (),
    invoice_no: str | None = None,
    adjusts_voucher_id: UUID | None = None,
    description: str,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        adjusts_voucher_id=adjusts_voucher_id,
        operation_code=operation,
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        invoice_form="1" if invoice_no else None,
        invoice_serial="C26TAY" if invoice_no else None,
        invoice_no=invoice_no,
        invoice_date=posting_date if invoice_no else None,
        description=description,
        lines=(
            SalesInvoiceLineIn(
                description="Hàng 7H-2a",
                item_id=ITEM_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(1),
                unit_price_fc=amount,
                amount_fc=amount,
                vat_rate=Decimal(10),
                vat_amount_fc=vat,
                account_id=accounts["5111" if kind != SalesInvoiceKind.RETURN else "521"],
                vat_account_id=accounts["33311"],
            ),
        ),
        settlements=settlements,
    )


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Tám chứng từ phủ các dòng của lưới: nháp; ghi sổ không tờ và quá hạn; ghi
    sổ có tờ đã cấp mã rồi bị trả lại một phần; ghi sổ có tờ mới nháp; tờ trả
    lại (đối trừ, không khoản nợ riêng); nháp USD; tờ đã phát hành rồi bị THAY
    THẾ với tờ thay còn nháp; chứng từ điều chỉnh thông tin HAI lần (hai tờ
    `DA_DIEU_CHINH` + một nháp)."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = SalesInvoiceService(session)
        einvoices = EInvoiceService(session)

        draft = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_A,
                posting_date=SEP_12,
                due_date=OCT_15,
                description="nháp",
            ),
            user_id=ACTOR_ID,
        )
        ids["draft"] = draft.id

        # Ghi sổ, không tờ HĐĐT, hạn 20/09 → thiếu hóa đơn và quá hạn tại 30/09.
        bare = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_B,
                posting_date=SEP_05,
                due_date=SEP_20,
                description="không tờ",
            ),
            user_id=ACTOR_ID,
        )
        service.post(bare.id, user_id=ACTOR_ID)
        ids["bare"] = bare.id

        # Ghi sổ, tờ đã cấp mã (số ghi ngược lên thân), hạn 15/10; rồi bị trả
        # lại một phần → còn phải thu nhích xuống.
        issued = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_A,
                posting_date=SEP_10,
                due_date=OCT_15,
                description="đã cấp mã",
            ),
            user_id=ACTOR_ID,
        )
        service.post(issued.id, user_id=ACTOR_ID)
        sheet = einvoices.create_draft(source_voucher_id=issued.id, invoice_form_id=FORM_ID)
        einvoices.issue(sheet.id, invoice_date=SEP_10)
        einvoices.confirm(sheet.id, tax_authority_code="M1-7H2A-0001")
        ids["issued"] = issued.id
        (debt,) = session.scalars(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == issued.id)
        ).all()
        returned = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_A,
                posting_date=SEP_12,
                due_date=None,
                kind=SalesInvoiceKind.RETURN,
                operation="tra-lai-hang-ban",
                amount=Decimal(300_000),
                vat=Decimal(30_000),
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=debt.id,
                        amount_fc=RETURNED,
                    ),
                ),
                description="trả lại một phần",
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)
        ids["returned"] = returned.id

        # Ghi sổ, tờ chỉ mới NHÁP → vẫn thiếu hóa đơn (tờ nháp không tính).
        drafted = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_B,
                posting_date=SEP_10,
                due_date=OCT_15,
                description="tờ nháp",
            ),
            user_id=ACTOR_ID,
        )
        service.post(drafted.id, user_id=ACTOR_ID)
        einvoices.create_draft(source_voucher_id=drafted.id, invoice_form_id=FORM_ID)
        ids["sheet_draft"] = drafted.id

        # Nháp USD có số hóa đơn gõ tay — dòng tổng phải tách nó khỏi VND.
        usd = _invoice(
            context,
            accounts,
            customer_id=CUSTOMER_B,
            posting_date=SEP_12,
            due_date=OCT_15,
            amount=Decimal(100),
            vat=Decimal(10),
            invoice_no="0000777",
            description="hóa đơn USD",
        ).model_copy(update={"currency_code": "USD", "exchange_rate": Decimal(25_000)})
        ids["usd"] = service.create(usd, user_id=ACTOR_ID).id

        # Tờ đã cấp mã rồi bị THAY THẾ; tờ thay còn NHÁP treo lên chính chứng từ
        # (review 7G-4 H-1) → vẫn thiếu hóa đơn, không lộ số của tờ cũ.
        replaced = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_B,
                posting_date=SEP_10,
                due_date=OCT_15,
                description="tờ thay",
            ),
            user_id=ACTOR_ID,
        )
        service.post(replaced.id, user_id=ACTOR_ID)
        replaced_sheet = einvoices.create_draft(
            source_voucher_id=replaced.id, invoice_form_id=FORM_ID
        )
        einvoices.issue(replaced_sheet.id, invoice_date=SEP_10)
        einvoices.confirm(replaced_sheet.id, tax_authority_code="M1-7H2A-0002")
        ErrorFlowService(session).apply(
            replaced_sheet.id,
            error_kind=ErrorKind.SAI_SO_TIEN,
            buyer_declared=False,
            notice_no="TBSS-7H2A-01",
            notice_date=SEP_10,
        )
        ids["replaced"] = replaced.id

        # Điều chỉnh THÔNG TIN hai lần: tờ 1 và tờ 2 đều `DA_DIEU_CHINH` (còn
        # sống, ngoài chỉ mục riêng phần), tờ 3 còn nháp — lưới phải ra MỘT dòng
        # và ký hiệu/số phải cùng một tờ (tờ 2, mới hơn) — review 7H-2a M-1.
        twice = service.create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_B,
                posting_date=SEP_10,
                due_date=OCT_15,
                description="điều chỉnh hai lần",
            ),
            user_id=ACTOR_ID,
        )
        service.post(twice.id, user_id=ACTOR_ID)
        first = einvoices.create_draft(source_voucher_id=twice.id, invoice_form_id=FORM_ID)
        einvoices.issue(first.id, invoice_date=SEP_10)
        einvoices.confirm(first.id, tax_authority_code="M1-7H2A-0003")
        second_outcome = ErrorFlowService(session).apply(
            first.id,
            error_kind=ErrorKind.SAI_THONG_TIN,
            buyer_declared=True,
            notice_no="TBSS-7H2A-02",
            notice_date=SEP_10,
        )
        assert second_outcome.adjustment is not None
        einvoices.issue(second_outcome.adjustment.id, invoice_date=SEP_12)
        einvoices.confirm(second_outcome.adjustment.id, tax_authority_code="M1-7H2A-0004")
        ErrorFlowService(session).apply(
            second_outcome.adjustment.id,
            error_kind=ErrorKind.SAI_THONG_TIN,
            buyer_declared=True,
            notice_no="TBSS-7H2A-03",
            notice_date=SEP_12,
        )
        ids["twice_adjusted"] = twice.id
        ids["twice_adjusted_second_sheet"] = second_outcome.adjustment.id
    return ids


@pytest.fixture(scope="module")
def voucher_ids(books: dict[str, UUID]) -> dict[str, UUID]:
    """Chỉ những khóa là CHỨNG TỪ — `books` mang thêm id một tờ hóa đơn để so."""
    return {name: value for name, value in books.items() if not name.endswith("_sheet")}


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        READER_ROLE,
        [permission_code("sales", "invoice", Action.VIEW)],
    )


@pytest.fixture(scope="module")
def other_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        OTHER_ROLE,
        [permission_code("master", "partners", Action.VIEW)],
    )


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


@pytest.fixture
def reader(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    books: dict[str, UUID],
    reader_role: str,
) -> dict[str, str]:
    return _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        reader_role,
        "luoi_ban_7h2a",
    )


def _fetch(client: TestClient, headers: dict[str, str], **params: object) -> dict[str, object]:
    response = client.get(
        "/api/v1/sales/invoices",
        params={"as_of": SEP_30.isoformat(), **{k: str(v) for k, v in params.items()}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _by_id(body: dict[str, object]) -> dict[str, dict[str, object]]:
    items = body["items"]
    assert isinstance(items, list)
    return {str(row["id"]): row for row in items}


def test_rows_carry_customer_invoice_and_remaining(
    client: TestClient,
    reader: dict[str, str],
    books: dict[str, UUID],
    voucher_ids: dict[str, UUID],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    rows = _by_id(_fetch(client, reader))
    assert set(rows) == {str(value) for value in voucher_ids.values()}

    draft = rows[str(books["draft"])]
    assert draft["customer_code"] == CUSTOMER_A_CODE
    assert draft["customer_name"] == f"Khách hàng {CUSTOMER_A_CODE}"
    assert Decimal(str(draft["total_fc"])) == TOTAL
    # Chưa ghi sổ → chưa có dòng sổ phụ → không có số còn nợ, không quá hạn.
    assert draft["remaining_fc"] is None
    assert draft["due_date"] == OCT_15.isoformat()
    assert draft["days_overdue"] is None
    assert draft["has_live_einvoice"] is False

    bare = rows[str(books["bare"])]
    assert bare["invoice_no"] is None
    assert bare["has_live_einvoice"] is False
    assert Decimal(str(bare["remaining_fc"])) == TOTAL
    assert bare["days_overdue"] == (SEP_30 - SEP_20).days

    # Tờ đã cấp mã: ký hiệu + số đọc từ TỜ (thân không có số gõ tay), tờ còn
    # sống; trả lại 330.000 vào 1.100.000 → còn 770.000. Tờ trả lại không mang
    # khoản nợ riêng.
    issued = rows[str(books["issued"])]
    assert issued["has_live_einvoice"] is True
    assert issued["invoice_no"] is None
    assert issued["einvoice_serial"] == SERIAL
    assert issued["einvoice_no"] is not None
    assert Decimal(str(issued["remaining_fc"])) == TOTAL - RETURNED
    assert issued["days_overdue"] is None
    returned = rows[str(books["returned"])]
    assert returned["kind"] == SalesInvoiceKind.RETURN
    assert returned["remaining_fc"] is None

    # Tờ mới nháp KHÔNG phải hóa đơn (không lộ số); số gõ tay trên nháp USD thì
    # vẫn hiện — hai nguồn, hai cột.
    sheet_draft = rows[str(books["sheet_draft"])]
    assert sheet_draft["has_live_einvoice"] is False
    assert sheet_draft["einvoice_no"] is None
    usd = rows[str(books["usd"])]
    assert usd["invoice_no"] == "0000777"
    assert usd["einvoice_serial"] is None

    # Tờ cũ đã bị thay thế + tờ thay còn nháp = chưa có hóa đơn hợp lệ; số của
    # tờ cũ KHÔNG được lộ lên lưới như thể còn hiệu lực.
    replaced = rows[str(books["replaced"])]
    assert replaced["has_live_einvoice"] is False
    assert replaced["einvoice_no"] is None

    # Hai tờ `DA_DIEU_CHINH` + một nháp trên một chứng từ: MỘT dòng lưới, và
    # ký hiệu/số là của tờ thứ hai (mới hơn) — cùng một tờ cho cả hai cột.
    twice = rows[str(books["twice_adjusted"])]
    assert twice["has_live_einvoice"] is True
    with unit_of_work(
        session_factory, posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ) as session:
        second = EInvoiceService(session).require(books["twice_adjusted_second_sheet"])
        assert second.invoice_no is not None
        assert twice["einvoice_no"] == second.invoice_no
        assert twice["einvoice_serial"] == SERIAL


def test_totals_cover_the_whole_filtered_set_not_the_page(
    client: TestClient, reader: dict[str, str]
) -> None:
    body = _fetch(client, reader, page_size=1)
    items = body["items"]
    assert isinstance(items, list) and len(items) == 1
    # Tám chứng từ, không phải nhiều hơn: chứng từ mang ba tờ HĐĐT vẫn là MỘT dòng.
    assert body["total"] == 8
    totals = body["totals"]
    assert isinstance(totals, list)
    by_currency = {str(row["currency_code"]): row for row in totals}
    assert set(by_currency) == {"USD", "VND"}
    vnd = by_currency["VND"]
    assert vnd["count"] == 7
    # Tờ trả lại TRỪ vào tổng bán — doanh thu ròng.
    assert Decimal(str(vnd["total_fc"])) == TOTAL * 6 - RETURNED
    assert Decimal(str(vnd["remaining_fc"])) == TOTAL * 4 + (TOTAL - RETURNED)
    usd = by_currency["USD"]
    assert usd["count"] == 1
    assert Decimal(str(usd["total_fc"])) == Decimal(110)
    assert Decimal(str(usd["remaining_fc"])) == Decimal(0)


def test_newest_first(client: TestClient, reader: dict[str, str], books: dict[str, UUID]) -> None:
    items = _fetch(client, reader)["items"]
    assert isinstance(items, list)
    dates = [str(row["posting_date"]) for row in items]
    assert dates == sorted(dates, reverse=True)
    assert str(items[-1]["id"]) == str(books["bare"])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": 1}, {"draft", "usd"}),
        # Đúng nhóm `chua-co-hoa-don` của BFF: đã ghi sổ, loại cần hóa đơn, không
        # tờ còn sống — tờ nháp không tính, tờ trả lại không cần hóa đơn.
        ({"einvoice": "missing"}, {"bare", "sheet_draft", "replaced"}),
        ({"overdue": "true"}, {"bare"}),
        ({"customer_id": CUSTOMER_B}, {"bare", "sheet_draft", "usd", "replaced", "twice_adjusted"}),
        ({"kind": SalesInvoiceKind.RETURN}, {"returned"}),
        ({"from_date": SEP_12.isoformat()}, {"draft", "returned", "usd"}),
        ({"to_date": SEP_05.isoformat()}, {"bare"}),
    ],
)
def test_each_filter_axis(
    client: TestClient,
    reader: dict[str, str],
    books: dict[str, UUID],
    params: dict[str, object],
    expected: set[str],
) -> None:
    rows = _by_id(_fetch(client, reader, **params))
    assert set(rows) == {str(books[name]) for name in expected}


def test_missing_einvoice_filter_agrees_with_the_pending_issues_tab(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Tab đếm và lưới liệt kê phải là cùng một tập — hai chỗ cùng đọc một hằng."""
    tab = client.get(
        "/api/v1/sales/pending-issues", params={"as_of": SEP_30.isoformat()}, headers=reader
    )
    assert tab.status_code == 200, tab.text
    groups = {str(group["code"]): group for group in tab.json()["groups"]}
    tab_count = int(groups["chua-co-hoa-don"]["count"])
    grid = _fetch(client, reader, einvoice="missing")
    assert grid["total"] == tab_count


def test_unknown_einvoice_filter_is_rejected(client: TestClient, reader: dict[str, str]) -> None:
    response = client.get("/api/v1/sales/invoices", params={"einvoice": "issued"}, headers=reader)
    assert response.status_code == 422, response.text


def test_overdue_follows_as_of(
    client: TestClient, reader: dict[str, str], books: dict[str, UUID]
) -> None:
    response = client.get(
        "/api/v1/sales/invoices",
        params={"as_of": SEP_10.isoformat(), "overdue": "true"},
        headers=reader,
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    rows = _by_id(_fetch(client, reader))
    assert rows[str(books["bare"])]["days_overdue"] == 10


def test_requires_sales_invoice_view(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    books: dict[str, UUID],
    other_role: str,
) -> None:
    headers = _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        other_role,
        "khong_quyen_ban_7h2a",
    )
    response = client.get("/api/v1/sales/invoices", headers=headers)
    assert response.status_code == 403, response.text


def test_period_filter(
    client: TestClient, reader: dict[str, str], books: dict[str, UUID], voucher_ids: dict[str, UUID]
) -> None:
    detail = client.get(f"/api/v1/sales/invoices/{books['issued']}", headers=reader)
    assert detail.status_code == 200, detail.text
    period_id = int(detail.json()["period_id"])
    rows = _by_id(_fetch(client, reader, period_id=period_id))
    assert set(rows) == {str(value) for value in voucher_ids.values()}
    assert _fetch(client, reader, period_id=999_999)["items"] == []


def test_other_branch_stays_invisible(
    client: TestClient,
    reader: dict[str, str],
    voucher_ids: dict[str, UUID],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    accounts: dict[str, int],
) -> None:
    """RLS lọc chi nhánh trước khi câu SELECT của lưới chạy — kể cả trong dòng tổng."""
    other = seed_posting_context(session_factory, dataset_alpha)
    other_scope = posting_scope(dataset_alpha, other, user_id=ACTOR_ID)
    with unit_of_work(session_factory, other_scope) as session:
        foreign = (
            SalesInvoiceService(session)
            .create(
                _invoice(
                    other,
                    accounts,
                    customer_id=CUSTOMER_A,
                    posting_date=SEP_12,
                    due_date=OCT_15,
                    amount=Decimal(777_000),
                    vat=Decimal(77_700),
                    description="chi nhánh khác",
                ),
                user_id=ACTOR_ID,
            )
            .id
        )
    body = _fetch(client, reader)
    assert str(foreign) not in _by_id(body)
    assert body["total"] == len(voucher_ids)


def test_adjustment_voucher_echoes_its_original(
    client: TestClient,
    reader: dict[str, str],
    books: dict[str, UUID],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """`GET /sales/invoices/{id}` vọng lại `adjusts_voucher_id` — form sửa chứng
    từ điều chỉnh PUT trọn bộ được và thẻ "Điều chỉnh chứng từ" chỉ ra tờ gốc."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        adjustment = SalesInvoiceService(session).create(
            _invoice(
                context,
                accounts,
                customer_id=CUSTOMER_A,
                posting_date=SEP_12,
                due_date=None,
                kind=SalesInvoiceKind.ADJUSTMENT_INCREASE,
                operation="dieu-chinh-tang-hoa-don",
                amount=Decimal(10_000),
                vat=Decimal(1_000),
                adjusts_voucher_id=books["issued"],
                description="điều chỉnh tăng",
            ),
            user_id=ACTOR_ID,
        )
    detail = client.get(f"/api/v1/sales/invoices/{adjustment.id}", headers=reader)
    assert detail.status_code == 200, detail.text
    assert detail.json()["adjusts_voucher_id"] == str(books["issued"])
    plain = client.get(f"/api/v1/sales/invoices/{books['issued']}", headers=reader)
    assert plain.json()["adjusts_voucher_id"] is None
