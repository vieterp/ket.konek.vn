"""Lưới chứng từ mua hàng (lát 7H-1, màn 01 design) — `GET /api/v1/purchase/invoices`.

Lưới phải nói được ba điều mà `/vouchers` không nói: NCC là ai, hóa đơn NCC đã
có chưa, và **còn phải trả bao nhiêu hiện nay** — con số cuối đọc từ dòng sổ phụ
của chính chứng từ nên một lượt đối trừ (trả lại hàng) phải làm nó nhích đúng
số đối trừ. Bộ lọc `overdue` phải là đúng định nghĩa nhóm `qua-han` của BFF
`pending-issues` để bấm tab rồi lọc ra cùng tập chứng từ. Dòng tổng tính trên
toàn tập lọc, không riêng trang.

Khối id 991x: `dataset_alpha` dùng chung cả phiên — helper `ensure_*` chạy trước
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
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
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
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_vendor, seed_purchase_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
READER_ROLE = "xem_luoi_mua_hang_7h1"
OTHER_ROLE = "khong_quyen_mua_hang_7h1"

SEP_05 = date(2026, 9, 5)
SEP_10 = date(2026, 9, 10)
SEP_12 = date(2026, 9, 12)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)
OCT_15 = date(2026, 10, 15)

VENDOR_A = 9961
VENDOR_B = 9962
VENDOR_A_CODE = "NCC-7H1-A"
VENDOR_B_CODE = "NCC-7H1-B"

GOODS = Decimal(1_000_000)
VAT = Decimal(100_000)
TOTAL = GOODS + VAT
RETURNED = Decimal(330_000)
FREIGHT = Decimal(80_000)


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
    codes = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_vendor(session, partner_id=VENDOR_A, code=VENDOR_A_CODE)
        ensure_vendor(session, partner_id=VENDOR_B, code=VENDOR_B_CODE)
    return codes


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    vendor_id: int,
    posting_date: date,
    due_date: date | None,
    vendor_invoice_status: int = VendorInvoiceStatus.RECEIVED,
    kind: int = PurchaseInvoiceKind.GOODS,
    operation: str = "mua-hang-hoa",
    amount: Decimal = GOODS,
    vat: Decimal = VAT,
    settlements: tuple[PurchaseSettlementIn, ...] = (),
    landed_costs: tuple[LandedCostIn, ...] = (),
    description: str,
) -> PurchaseInvoiceIn:
    received = vendor_invoice_status == VendorInvoiceStatus.RECEIVED
    return PurchaseInvoiceIn(
        kind=kind,
        operation_code=operation,
        vendor_id=vendor_id,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        vendor_invoice_status=vendor_invoice_status,
        vendor_invoice_form="1",
        vendor_invoice_serial="C26TAA" if received else None,
        vendor_invoice_no="0000901" if received else None,
        vendor_invoice_date=posting_date if received else None,
        landed_cost_allocation=LandedCostAllocation.BY_VALUE,
        description=description,
        lines=(
            PurchaseInvoiceLineIn(
                description="Hàng 7H-1",
                quantity=Decimal(1),
                unit_price_fc=amount,
                amount_fc=amount,
                vat_rate=Decimal(10) if received else Decimal(0),
                vat_amount_fc=vat if received else Decimal(0),
                account_id=accounts["156"],
                vat_account_id=accounts["1331"] if received else None,
            ),
        ),
        settlements=settlements,
        landed_costs=landed_costs,
    )


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Bốn chứng từ phủ bốn dòng của lưới: nháp, thiếu hóa đơn quá hạn, đã trả
    một phần chưa tới hạn, và tờ trả lại (đối trừ, không có khoản nợ riêng)."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = PurchaseInvoiceService(session)

        draft = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=VENDOR_A,
                posting_date=SEP_12,
                due_date=OCT_15,
                description="nháp",
            ),
            user_id=ACTOR_ID,
        )
        ids["draft"] = draft.id

        # Hàng về trước hóa đơn về sau, hạn 20/09 → quá hạn tại 30/09.
        not_yet = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=VENDOR_B,
                posting_date=SEP_05,
                due_date=SEP_20,
                vendor_invoice_status=VendorInvoiceStatus.NOT_YET,
                description="hóa đơn về sau",
            ),
            user_id=ACTOR_ID,
        )
        service.post(not_yet.id, user_id=ACTOR_ID)
        ids["not_yet"] = not_yet.id

        # Đã nhận hóa đơn, hạn 15/10, kèm khoản vận chuyển do NCC B thu — tức
        # hóa đơn này có HAI dòng sổ phụ (NCC A + NCC B) cùng `document_id`;
        # rồi trả lại một phần → còn nợ NCC A nhích xuống.
        received = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=VENDOR_A,
                posting_date=SEP_10,
                due_date=OCT_15,
                landed_costs=(
                    LandedCostIn(
                        description="Vận chuyển",
                        vendor_id=VENDOR_B,
                        credit_account_id=accounts["331"],
                        amount_fc=FREIGHT,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                    ),
                ),
                description="đã nhận hóa đơn",
            ),
            user_id=ACTOR_ID,
        )
        service.post(received.id, user_id=ACTOR_ID)
        ids["received"] = received.id
        (debt,) = session.scalars(
            select(ArApLedgerEntry).where(
                ArApLedgerEntry.document_id == received.id,
                ArApLedgerEntry.partner_id == VENDOR_A,
            )
        ).all()
        returned = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=VENDOR_A,
                posting_date=SEP_12,
                due_date=None,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                amount=Decimal(300_000),
                vat=Decimal(30_000),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
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

        # Hóa đơn USD còn nháp — dòng tổng phải tách nó khỏi VND.
        usd = _invoice(
            context,
            accounts,
            vendor_id=VENDOR_B,
            posting_date=SEP_12,
            due_date=OCT_15,
            amount=Decimal(100),
            vat=Decimal(10),
            description="hóa đơn USD",
        ).model_copy(update={"currency_code": "USD", "exchange_rate": Decimal(25_000)})
        ids["usd"] = service.create(usd, user_id=ACTOR_ID).id
    return ids


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        READER_ROLE,
        [permission_code("purchase", "invoice", Action.VIEW)],
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
        "luoi_mua_7h1",
    )


def _fetch(client: TestClient, headers: dict[str, str], **params: object) -> dict[str, object]:
    response = client.get(
        "/api/v1/purchase/invoices",
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


def test_rows_carry_vendor_invoice_and_remaining(
    client: TestClient, reader: dict[str, str], books: dict[str, UUID]
) -> None:
    rows = _by_id(_fetch(client, reader))
    assert set(rows) == {str(value) for value in books.values()}

    draft = rows[str(books["draft"])]
    assert draft["vendor_code"] == VENDOR_A_CODE
    assert draft["vendor_name"] == f"Nhà cung cấp {VENDOR_A_CODE}"
    assert Decimal(str(draft["total_fc"])) == TOTAL
    # Chưa ghi sổ → chưa có dòng sổ phụ → không có số còn nợ, không quá hạn.
    assert draft["remaining_fc"] is None
    assert draft["due_date"] == OCT_15.isoformat()
    assert draft["days_overdue"] is None

    not_yet = rows[str(books["not_yet"])]
    assert not_yet["vendor_invoice_status"] == VendorInvoiceStatus.NOT_YET
    assert not_yet["vendor_invoice_no"] is None
    assert Decimal(str(not_yet["remaining_fc"])) == GOODS
    assert not_yet["days_overdue"] == (SEP_30 - SEP_20).days

    # Trả lại 330.000 vào hóa đơn 1.100.000 → còn 770.000 với NCC A; khoản
    # vận chuyển 80.000 nợ NCC B là dòng sổ phụ khác cùng chứng từ — KHÔNG
    # nhân đôi dòng lưới, không cộng vào "còn phải trả" của hóa đơn. Tờ trả lại
    # không mang khoản nợ riêng.
    received = rows[str(books["received"])]
    assert received["vendor_invoice_serial"] == "C26TAA"
    assert Decimal(str(received["total_fc"])) == TOTAL + FREIGHT
    assert Decimal(str(received["remaining_fc"])) == TOTAL - RETURNED
    assert received["days_overdue"] is None
    returned = rows[str(books["returned"])]
    assert returned["kind"] == PurchaseInvoiceKind.RETURN
    assert returned["remaining_fc"] is None


def test_totals_cover_the_whole_filtered_set_not_the_page(
    client: TestClient, reader: dict[str, str]
) -> None:
    body = _fetch(client, reader, page_size=1)
    items = body["items"]
    assert isinstance(items, list) and len(items) == 1
    # Năm chứng từ, không phải sáu: hóa đơn có hai dòng sổ phụ vẫn là MỘT dòng.
    assert body["total"] == 5
    totals = body["totals"]
    assert isinstance(totals, list)
    by_currency = {str(row["currency_code"]): row for row in totals}
    # Một dòng tổng MỖI tiền tệ — USD không cộng lẫn vào VND.
    assert set(by_currency) == {"USD", "VND"}
    vnd = by_currency["VND"]
    assert vnd["count"] == 4
    # Tờ trả lại TRỪ vào tổng mua — giá trị mua ròng, không phải cộng dồn giá trị tuyệt đối.
    assert Decimal(str(vnd["total_fc"])) == TOTAL + GOODS + (TOTAL + FREIGHT) - RETURNED
    assert Decimal(str(vnd["remaining_fc"])) == GOODS + (TOTAL - RETURNED)
    usd = by_currency["USD"]
    assert usd["count"] == 1
    assert Decimal(str(usd["total_fc"])) == Decimal(110)
    assert Decimal(str(usd["remaining_fc"])) == Decimal(0)


def test_newest_first(client: TestClient, reader: dict[str, str], books: dict[str, UUID]) -> None:
    items = _fetch(client, reader)["items"]
    assert isinstance(items, list)
    dates = [str(row["posting_date"]) for row in items]
    assert dates == sorted(dates, reverse=True)
    assert str(items[-1]["id"]) == str(books["not_yet"])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": 1}, {"draft", "usd"}),
        ({"vendor_invoice_status": VendorInvoiceStatus.NOT_YET}, {"not_yet"}),
        ({"overdue": "true"}, {"not_yet"}),
        ({"vendor_id": VENDOR_B}, {"not_yet", "usd"}),
        ({"kind": PurchaseInvoiceKind.RETURN}, {"returned"}),
        ({"from_date": SEP_12.isoformat()}, {"draft", "returned", "usd"}),
        ({"to_date": SEP_05.isoformat()}, {"not_yet"}),
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


def test_overdue_follows_as_of(
    client: TestClient, reader: dict[str, str], books: dict[str, UUID]
) -> None:
    # Tại 10/09 hạn 20/09 chưa qua → không chứng từ nào quá hạn.
    response = client.get(
        "/api/v1/purchase/invoices",
        params={"as_of": SEP_10.isoformat(), "overdue": "true"},
        headers=reader,
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    rows = _by_id(_fetch(client, reader))
    assert rows[str(books["not_yet"])]["days_overdue"] == 10


def test_requires_purchase_invoice_view(
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
        "khong_quyen_mua_7h1",
    )
    response = client.get("/api/v1/purchase/invoices", headers=headers)
    assert response.status_code == 403, response.text


def test_period_filter(client: TestClient, reader: dict[str, str], books: dict[str, UUID]) -> None:
    detail = client.get(f"/api/v1/purchase/invoices/{books['received']}", headers=reader)
    assert detail.status_code == 200, detail.text
    period_id = int(detail.json()["period_id"])
    rows = _by_id(_fetch(client, reader, period_id=period_id))
    assert set(rows) == {str(value) for value in books.values()}
    assert _fetch(client, reader, period_id=999_999)["items"] == []


def test_other_branch_stays_invisible(
    client: TestClient,
    reader: dict[str, str],
    books: dict[str, UUID],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    accounts: dict[str, int],
) -> None:
    """RLS lọc chi nhánh trước khi câu SELECT của lưới chạy — người dùng chỉ được
    gán chi nhánh này không thấy chứng từ của chi nhánh khác, kể cả trong dòng tổng."""
    other = seed_posting_context(session_factory, dataset_alpha)
    other_scope = posting_scope(dataset_alpha, other, user_id=ACTOR_ID)
    with unit_of_work(session_factory, other_scope) as session:
        foreign = (
            PurchaseInvoiceService(session)
            .create(
                _invoice(
                    other,
                    accounts,
                    vendor_id=VENDOR_A,
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
    assert body["total"] == len(books)
