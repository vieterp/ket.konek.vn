"""Endpoint phiếu kho qua HTTP (lát 8A): tạo/đọc/sửa theo ba đường dẫn, ghi sổ
qua endpoint chứng từ dùng chung, tồn kho, sắp xếp lại thứ tự, và hai nhóm
"chưa nhập/xuất kho" của BFF việc còn thiếu.

Quyền tách theo loại phiếu: người chỉ có `inventory.receipt.*` không lập được
phiếu xuất (403), và `kind` trên thân lệch đường dẫn là 422.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from inventory_support import (
    GOODS_ITEM_ID,
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_BOX_ID,
    UNIT_PIECE_ID,
    movements_of,
    seed_inventory_package_data,
)
from ket.api.dependencies import BRANCH_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.purchase.models import VendorInvoiceStatus
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_vendor, seed_purchase_package_data
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
KEEPER_ROLE = "thu_kho_8a"
RECEIPT_ONLY_ROLE = "chi_nhap_kho_8a"
TRADE_ROLE = "xem_mua_ban_8a"
VENDOR_ID = 8301
CUSTOMER_ID = 8302
SALESPERSON_ID = 8303
SEP_10 = date(2026, 9, 10)
SEP_12 = date(2026, 9, 12)
SEP_30 = date(2026, 9, 30)


def _inventory_codes(names: tuple[str, ...]) -> list[str]:
    return [
        permission_code("inventory", name, action)
        for name in names
        for action in (
            Action.VIEW,
            Action.CREATE,
            Action.EDIT,
            Action.DELETE,
            Action.POST,
            Action.UNPOST,
        )
    ]


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
    codes = seed_inventory_package_data(session_factory, dataset_alpha, context)
    codes |= seed_purchase_package_data(session_factory, dataset_alpha, context)
    codes |= seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_vendor(session, partner_id=VENDOR_ID, code="NCC-8A-API")
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-8A-API")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-8A-API")
    return codes


@pytest.fixture(scope="module")
def keeper_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        KEEPER_ROLE,
        _inventory_codes(("receipt", "issue", "transfer")),
    )


@pytest.fixture(scope="module")
def receipt_only_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory, dataset_alpha, RECEIPT_ONLY_ROLE, _inventory_codes(("receipt",))
    )


@pytest.fixture(scope="module")
def trade_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        TRADE_ROLE,
        [
            permission_code("purchase", "invoice", Action.VIEW),
            permission_code("sales", "invoice", Action.VIEW),
        ],
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
def keeper_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    keeper_role: str,
) -> dict[str, str]:
    return _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        keeper_role,
        "thu_kho",
    )


def _receipt_body(
    context: PostingContext, accounts: dict[str, int], *, kind: int = 0, quantity: str = "3"
) -> dict[str, Any]:
    return {
        "kind": kind,
        "operation_code": "nhap-thanh-pham" if kind == 0 else "xuat-nvl-san-xuat",
        "warehouse_id": MAIN_WAREHOUSE_ID,
        "branch_id": context.branch_id,
        "document_date": SEP_10.isoformat(),
        "posting_date": SEP_10.isoformat(),
        "currency_code": "VND",
        "exchange_rate": "1",
        "description": "phiếu kho qua HTTP",
        "lines": [
            {
                "item_id": GOODS_ITEM_ID,
                "unit_id": UNIT_BOX_ID,
                "quantity": quantity,
                "unit_cost_fc": "240000",
                "debit_account_id": accounts["155"],
                "credit_account_id": accounts["154"],
            }
        ],
    }


def _post_json(
    client: TestClient, headers: dict[str, str], path: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return client.post(path, json=body, headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex})


def test_receipt_round_trip_posts_through_the_shared_endpoint_and_shows_stock(
    client: TestClient,
    keeper_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    created = _post_json(
        client, keeper_headers, "/api/v1/inventory/receipts", _receipt_body(context, accounts)
    )
    assert created.status_code == 201, created.text
    voucher = created.json()
    assert voucher["voucher_no"].startswith("NK26-")
    assert voucher["kind"] == 0
    (line,) = voucher["lines"]
    assert Decimal(line["base_quantity"]) == Decimal(36)
    assert Decimal(line["amount_fc"]) == Decimal(720_000)

    read = client.get(f"/api/v1/inventory/receipts/{voucher['id']}", headers=keeper_headers)
    assert read.status_code == 200 and read.json()["id"] == voucher["id"]

    updated = client.put(
        f"/api/v1/inventory/receipts/{voucher['id']}",
        json={
            **_receipt_body(context, accounts, quantity="5"),
            "row_version": voucher["row_version"],
        },
        headers=keeper_headers,
    )
    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["lines"][0]["base_quantity"]) == Decimal(60)

    posted = _post_json(client, keeper_headers, f"/api/v1/vouchers/{voucher['id']}/actions/post")
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == 2

    stock = client.get(
        "/api/v1/inventory/stock",
        params={
            "as_of": SEP_30.isoformat(),
            "item_id": GOODS_ITEM_ID,
            "warehouse_id": MAIN_WAREHOUSE_ID,
        },
        headers=keeper_headers,
    )
    assert stock.status_code == 200, stock.text
    rows = stock.json()["items"]
    assert any(Decimal(row["on_hand"]) >= Decimal(60) for row in rows)


def test_kind_must_match_the_path_and_permissions_are_per_kind(
    client: TestClient,
    keeper_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
    receipt_only_role: str,
) -> None:
    mismatch = _post_json(
        client, keeper_headers, "/api/v1/inventory/issues", _receipt_body(context, accounts)
    )
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["violations"][0]["code"] == "inventory.kind_path_mismatch"

    receipt_only = _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        receipt_only_role,
        "chi_nhap",
    )
    forbidden = _post_json(
        client, receipt_only, "/api/v1/inventory/issues", _receipt_body(context, accounts, kind=1)
    )
    assert forbidden.status_code == 403, forbidden.text
    allowed = _post_json(
        client, receipt_only, "/api/v1/inventory/receipts", _receipt_body(context, accounts)
    )
    assert allowed.status_code == 201, allowed.text

    # Quyền xem theo LOẠI đã lưu, không theo đường dẫn (M-2): phiếu xuất do thủ
    # kho lập, người chỉ có `receipt.view` gọi `/receipts/{id}` vẫn 403.
    issue_body = _receipt_body(context, accounts, kind=1)
    del issue_body["lines"][0]["unit_cost_fc"]
    issue = _post_json(client, keeper_headers, "/api/v1/inventory/issues", issue_body)
    assert issue.status_code == 201, issue.text
    peek = client.get(f"/api/v1/inventory/receipts/{issue.json()['id']}", headers=receipt_only)
    assert peek.status_code == 403, peek.text


def test_reorder_day_endpoint_renumbers_the_key(
    client: TestClient,
    keeper_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    ids: list[UUID] = []
    for _ in range(2):
        body = _receipt_body(context, accounts)
        body["warehouse_id"] = SECOND_WAREHOUSE_ID
        body["posting_date"] = body["document_date"] = SEP_12.isoformat()
        created = _post_json(client, keeper_headers, "/api/v1/inventory/receipts", body)
        assert created.status_code == 201, created.text
        posted = _post_json(
            client, keeper_headers, f"/api/v1/vouchers/{created.json()['id']}/actions/post"
        )
        assert posted.status_code == 200, posted.text
        ids.append(UUID(created.json()["id"]))
    with unit_of_work(
        session_factory, posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ) as session:
        movement_ids = [movements_of(session, voucher_id)[0].id for voucher_id in ids]
    response = _post_json(
        client,
        keeper_headers,
        "/api/v1/inventory/movements/actions/reorder-day",
        {
            "branch_id": context.branch_id,
            "warehouse_id": SECOND_WAREHOUSE_ID,
            "item_id": GOODS_ITEM_ID,
            "posting_date": SEP_12.isoformat(),
            "ordered_movement_ids": list(reversed(movement_ids)),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["reordered"] == 2
    assert response.json()["marked_from"] == SEP_12.isoformat()


def test_pending_issues_expose_missing_stock_vouchers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
    trade_role: str,
) -> None:
    """Hóa đơn mua trên TK 1568 (không theo kho) không kho → "chưa nhập kho";
    hóa đơn bán không bật kiêm phiếu xuất → "chưa xuất kho"; hóa đơn mua có kho
    sinh phiếu → không vào nhóm."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        purchase = PurchaseInvoiceService(session)
        purchase_ids: dict[bool, UUID] = {}
        for with_warehouse in (False, True):
            invoice = purchase.create(
                PurchaseInvoiceIn(
                    kind=0,
                    operation_code="mua-hang-hoa",
                    vendor_id=VENDOR_ID,
                    payable_account_id=accounts["331"],
                    branch_id=context.branch_id,
                    document_date=SEP_10,
                    posting_date=SEP_10,
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
                    vendor_invoice_no="0000999",
                    description="mua chưa nhập kho" if not with_warehouse else "mua có kho",
                    lines=(
                        PurchaseInvoiceLineIn(
                            description="Hàng A",
                            item_id=GOODS_ITEM_ID,
                            unit_id=UNIT_PIECE_ID,
                            warehouse_id=MAIN_WAREHOUSE_ID if with_warehouse else None,
                            quantity=Decimal(2),
                            unit_price_fc=Decimal(100_000),
                            amount_fc=Decimal(200_000),
                            vat_rate=Decimal(0),
                            vat_amount_fc=Decimal(0),
                            account_id=accounts["156" if with_warehouse else "1568"],
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
            purchase.post(invoice.id, user_id=ACTOR_ID)
            purchase_ids[with_warehouse] = invoice.id
        missing_id, stocked_id = purchase_ids[False], purchase_ids[True]
        sales = SalesInvoiceService(session)
        invoice = sales.create(
            SalesInvoiceIn(
                kind=0,
                operation_code="ban-hang-hoa",
                customer_id=CUSTOMER_ID,
                receivable_account_id=accounts["131"],
                branch_id=context.branch_id,
                document_date=SEP_10,
                posting_date=SEP_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                salesperson_id=SALESPERSON_ID,
                invoice_no="0000998",
                description="bán chưa xuất kho",
                is_stock_issue=False,
                lines=(
                    SalesInvoiceLineIn(
                        description="Hàng A",
                        item_id=GOODS_ITEM_ID,
                        unit_id=UNIT_PIECE_ID,
                        quantity=Decimal(1),
                        unit_price_fc=Decimal(150_000),
                        amount_fc=Decimal(150_000),
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["5111"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        sales.post(invoice.id, user_id=ACTOR_ID)

    headers = _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        trade_role,
        "mua_ban",
    )
    purchase_tab = client.get(
        "/api/v1/purchase/pending-issues", params={"as_of": SEP_30.isoformat()}, headers=headers
    )
    assert purchase_tab.status_code == 200, purchase_tab.text
    groups = {group["code"]: group for group in purchase_tab.json()["groups"]}
    assert groups["chua-nhap-kho"]["next_action"] == "stock-in"
    assert groups["chua-nhap-kho"]["count"] >= 1
    assert all(row["source_label"] == "Hóa đơn mua" for row in groups["chua-nhap-kho"]["sample"])
    listed = {row["voucher_id"] for row in groups["chua-nhap-kho"]["sample"]}
    assert str(missing_id) in listed
    assert str(stocked_id) not in listed

    sales_tab = client.get(
        "/api/v1/sales/pending-issues", params={"as_of": SEP_30.isoformat()}, headers=headers
    )
    assert sales_tab.status_code == 200, sales_tab.text
    sales_groups = {group["code"]: group for group in sales_tab.json()["groups"]}
    assert sales_groups["chua-xuat-kho"]["next_action"] == "stock-out"
    assert sales_groups["chua-xuat-kho"]["count"] >= 1
