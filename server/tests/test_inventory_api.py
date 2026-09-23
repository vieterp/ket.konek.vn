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
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from inventory_support import (
    GOODS_ITEM_ID,
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_BOX_ID,
    UNIT_PIECE_ID,
    enable_keeper,
    fresh_item,
    in_movement,
    movements_of,
    out_movement,
    post_receipt,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.api.dependencies import BRANCH_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.config.catalog import STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.periods.models import InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.inventory.models import CostState, InventoryMovement
from ket.modules.inventory.service import SPECIFIC_SOURCE_MISMATCH_CODE
from ket.modules.purchase.models import VendorInvoiceStatus
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.sales.models import SalesInvoice
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SPECIFIC_SOURCE_KIND_CODE, SalesInvoiceService
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
        [
            *_inventory_codes(("receipt", "issue", "transfer", "assembly")),
            permission_code("inventory", "costing", Action.VIEW),
        ],
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


# ------------------------------------------------------------------ lát 8C-2


def _assembly_body(
    context: PostingContext, *, component_item_id: int, product_item_id: int
) -> dict[str, Any]:
    return {
        "kind": 3,
        "operation_code": "lap-rap",
        "warehouse_id": MAIN_WAREHOUSE_ID,
        "branch_id": context.branch_id,
        "document_date": SEP_10.isoformat(),
        "posting_date": SEP_10.isoformat(),
        "currency_code": "VND",
        "exchange_rate": "1",
        "description": "lắp ráp qua HTTP",
        "lines": [
            {
                "item_id": product_item_id,
                "unit_id": UNIT_PIECE_ID,
                "quantity": "1",
                "is_product": True,
            },
            {"item_id": component_item_id, "unit_id": UNIT_PIECE_ID, "quantity": "2"},
        ],
    }


def test_assembly_routes_need_the_assembly_permission_and_uncosted_lists_the_voucher(
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
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none")
        component = fresh_item(session, "API-C")
        product = fresh_item(session, "API-P")
    body = _assembly_body(context, component_item_id=component, product_item_id=product)

    receipt_only = _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        receipt_only_role,
        "chi_nhap_lr",
    )
    assert _post_json(client, receipt_only, "/api/v1/inventory/assemblies", body).status_code == 403

    created = _post_json(client, keeper_headers, "/api/v1/inventory/assemblies", body)
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["kind"] == 3
    assert payload["voucher_no"].startswith("LR26-")
    assert [line["is_product"] for line in payload["lines"]] == [True, False]
    # Đường dẫn phải khớp loại: thân lắp ráp gửi vào `/disassemblies` bị từ chối.
    mismatch = _post_json(client, keeper_headers, "/api/v1/inventory/disassemblies", body)
    assert mismatch.status_code == 422, mismatch.text

    posted = _post_json(client, keeper_headers, f"/api/v1/vouchers/{payload['id']}/actions/post")
    assert posted.status_code == 200, posted.text
    uncosted = client.get(
        "/api/v1/inventory/costing/uncosted",
        params={"date_from": SEP_10.isoformat(), "date_to": SEP_10.isoformat()},
        headers=keeper_headers,
    )
    assert uncosted.status_code == 200, uncosted.text
    listed = {row["voucher_id"]: row for row in uncosted.json()["vouchers"]}
    assert listed[payload["id"]]["document_type"] == "LR"
    assert listed[payload["id"]]["movements"] == 2
    assert uncosted.json()["count"] >= 1


def _stock_sale(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    source_movement_id: int | None,
    is_stock_issue: bool = True,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=0,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=SEP_12,
        posting_date=SEP_12,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        invoice_no="0000997",
        description="bán đích danh (8C-2)",
        is_stock_issue=is_stock_issue,
        lines=(
            SalesInvoiceLineIn(
                description="Hàng đích danh",
                item_id=item_id,
                unit_id=UNIT_PIECE_ID,
                warehouse_id=MAIN_WAREHOUSE_ID,
                quantity=Decimal(2),
                unit_price_fc=Decimal(150_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(0),
                vat_amount_fc=Decimal(0),
                account_id=accounts["5111"],
                cogs_account_id=accounts["632"],
                inventory_account_id=accounts["156"],
                source_movement_id=source_movement_id,
            ),
        ),
    )


def test_sales_line_names_the_specific_receipt_and_the_pending_tab_shows_uncosted_cogs(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
    trade_role: str,
) -> None:
    """Đích danh trên dòng bán (ADR-027): nguồn đúng → XK sinh mang nguồn, engine
    tính ngay giá lần nhập, `cogs_posted` lật; nguồn sai mã hàng → ghi sổ hóa đơn
    422 `specific_source_mismatch`; không nguồn → chờ giá và hóa đơn vào nhóm
    "chưa tính giá" của tab việc còn thiếu (FR-STK-008)."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    headers = _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        trade_role,
        "mua_ban_8c2",
    )
    with unit_of_work(session_factory, scope) as session:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none")
        set_valuation_method(session, context, InventoryValuationMethod.SPECIFIC.value)
        try:
            item, other = fresh_item(session, "SAL-SP"), fresh_item(session, "SAL-OTHER")
            receipt = post_receipt(
                session,
                context,
                accounts,
                item_id=item,
                posting_date=SEP_10,
                quantity=Decimal(5),
                unit_cost=Decimal(40_000),
            )
            other_receipt = post_receipt(
                session,
                context,
                accounts,
                item_id=other,
                posting_date=SEP_10,
                quantity=Decimal(5),
                unit_cost=Decimal(1),
            )
            sales = SalesInvoiceService(session)

            # Không kiêm phiếu xuất kho mà chỉ nguồn → hình thức sai ở sales.
            with pytest.raises(PostingValidationError) as caught:
                sales.create(
                    _stock_sale(
                        context,
                        accounts,
                        item_id=item,
                        source_movement_id=in_movement(session, receipt).id,
                        is_stock_issue=False,
                    ),
                    user_id=ACTOR_ID,
                )
            assert [v.code for v in caught.value.violations] == [SPECIFIC_SOURCE_KIND_CODE]

            # Nguồn là lần nhập của mã hàng KHÁC → module kho từ chối lúc sinh phiếu.
            wrong = sales.create(
                _stock_sale(
                    context,
                    accounts,
                    item_id=item,
                    source_movement_id=in_movement(session, other_receipt).id,
                ),
                user_id=ACTOR_ID,
            )
            with pytest.raises(PostingValidationError) as caught:
                sales.post(wrong.id, user_id=ACTOR_ID, acknowledged_warnings=True)
            assert [v.code for v in caught.value.violations] == [SPECIFIC_SOURCE_MISMATCH_CODE]

            # Không nguồn → ghi sổ được, phiếu xuất chờ giá.
            waiting = sales.create(
                _stock_sale(context, accounts, item_id=item, source_movement_id=None),
                user_id=ACTOR_ID,
            )
            sales.post(waiting.id, user_id=ACTOR_ID, acknowledged_warnings=True)

            # Nguồn đúng → phiếu xuất mang nguồn, engine tính giá lần nhập.
            named = sales.create(
                _stock_sale(
                    context,
                    accounts,
                    item_id=item,
                    source_movement_id=in_movement(session, receipt).id,
                ),
                user_id=ACTOR_ID,
            )
            sales.post(named.id, user_id=ACTOR_ID, acknowledged_warnings=True)
            run_engine(session, context)
            issue_id = session.scalar(
                select(InventoryMovement.voucher_id).where(
                    InventoryMovement.source_movement_id == in_movement(session, receipt).id
                )
            )
            assert issue_id is not None
            out = out_movement(session, issue_id)
            assert out.cost_state == CostState.COSTED
            assert out.amount == Decimal("80000.00")
            named_body = session.get(SalesInvoice, named.id)
            waiting_body = session.get(SalesInvoice, waiting.id)
            assert named_body is not None and named_body.cogs_posted is True
            assert waiting_body is not None and waiting_body.cogs_posted is False
        finally:
            set_valuation_method(
                session, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value
            )

    tab = client.get(
        "/api/v1/sales/pending-issues", params={"as_of": SEP_30.isoformat()}, headers=headers
    )
    assert tab.status_code == 200, tab.text
    groups = {group["code"]: group for group in tab.json()["groups"]}
    assert groups["chua-tinh-gia"]["next_action"] == "run-costing"
    listed = {row["voucher_id"] for row in groups["chua-tinh-gia"]["sample"]}
    assert str(waiting.id) in listed
    assert str(named.id) not in listed


# ------------------------------------------------- lát 8D: HTTP của ba cửa mới


@pytest.fixture(scope="module")
def count_sheet_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        "kk_kho_8d",
        [
            *_inventory_codes(("receipt", "issue", "count_sheet")),
            permission_code("keeper", "warehouse_book", Action.VIEW),
            permission_code("keeper", "warehouse_book", Action.POST),
        ],
    )


@pytest.fixture
def count_sheet_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    count_sheet_role: str,
) -> dict[str, str]:
    return _headers(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        count_sheet_role,
        "kiem_ke",
    )


def test_availability_endpoint_reports_stock_minus_commitment(
    client: TestClient,
    keeper_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    created = _post_json(
        client, keeper_headers, "/api/v1/inventory/receipts", _receipt_body(context, accounts)
    )
    assert created.status_code == 201, created.text
    posted = _post_json(
        client, keeper_headers, f"/api/v1/vouchers/{created.json()['id']}/actions/post"
    )
    assert posted.status_code == 200, posted.text

    response = client.get(
        "/api/v1/inventory/availability",
        params={"as_of": SEP_30.isoformat(), "item_ids": [GOODS_ITEM_ID]},
        headers=keeper_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_commitment_source"] is True
    row = next(item for item in body["items"] if item["item_id"] == GOODS_ITEM_ID)
    assert Decimal(row["available_to_promise"]) == Decimal(row["on_hand"]) - Decimal(
        row["committed"]
    )


def test_count_sheet_flow_over_http(
    client: TestClient,
    keeper_headers: dict[str, str],
    count_sheet_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Bốn lượt gọi của U8: lập → nhập số đếm → chỉ chênh lệch → duyệt."""
    created = _post_json(
        client, keeper_headers, "/api/v1/inventory/receipts", _receipt_body(context, accounts)
    )
    assert created.status_code == 201, created.text
    posted = _post_json(
        client, keeper_headers, f"/api/v1/vouchers/{created.json()['id']}/actions/post"
    )
    assert posted.status_code == 200, posted.text

    sheet = _post_json(
        client,
        count_sheet_headers,
        "/api/v1/inventory/count-sheets",
        {
            "branch_id": context.branch_id,
            "warehouse_id": MAIN_WAREHOUSE_ID,
            "count_date": SEP_30.isoformat(),
            "item_ids": [GOODS_ITEM_ID],
        },
    )
    assert sheet.status_code == 201, sheet.text
    sheet_id = sheet.json()["id"]
    assert sheet.json()["sheet_no"].startswith("KK26-")
    line = next(row for row in sheet.json()["lines"] if not row["is_custodial"])
    counted = Decimal(line["book_qty"]) + Decimal(2)

    saved = client.put(
        f"/api/v1/inventory/count-sheets/{sheet_id}/counts",
        json={"lines": [{"line_no": line["line_no"], "counted_qty": str(counted)}]},
        headers=count_sheet_headers,
    )
    assert saved.status_code == 200, saved.text

    differences = client.get(
        f"/api/v1/inventory/count-sheets/{sheet_id}/differences", headers=count_sheet_headers
    )
    assert differences.status_code == 200, differences.text
    rows = differences.json()["differences"]
    assert len(rows) == 1
    assert Decimal(rows[0]["difference"]) == Decimal(2)

    applied = _post_json(
        client,
        count_sheet_headers,
        f"/api/v1/inventory/count-sheets/{sheet_id}/actions/apply-differences",
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["receipt_voucher_id"] is not None
    assert applied.json()["issue_voucher_id"] is None

    listed = client.get(
        "/api/v1/inventory/count-sheets",
        params={"warehouse_id": MAIN_WAREHOUSE_ID},
        headers=count_sheet_headers,
    )
    assert listed.status_code == 200, listed.text
    assert any(row["id"] == sheet_id for row in listed.json()["items"])


def test_warehouse_keeper_queue_and_book_over_http(
    client: TestClient,
    keeper_headers: dict[str, str],
    count_sheet_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        enable_keeper(session)
    try:
        created = _post_json(
            client, keeper_headers, "/api/v1/inventory/receipts", _receipt_body(context, accounts)
        )
        assert created.status_code == 201, created.text
        voucher_id = created.json()["id"]
        posted = _post_json(client, keeper_headers, f"/api/v1/vouchers/{voucher_id}/actions/post")
        assert posted.status_code == 200, posted.text

        queue = client.get("/api/v1/warehouse-keeper/queue", headers=count_sheet_headers)
        assert queue.status_code == 200, queue.text
        assert voucher_id in {row["voucher_id"] for row in queue.json()["items"]}

        booked = _post_json(
            client,
            count_sheet_headers,
            "/api/v1/warehouse-keeper/queue/actions/book",
            {"voucher_ids": [voucher_id], "book_date_mode": "posting_date"},
        )
        assert booked.status_code == 200, booked.text
        assert booked.json()["booked_rows"] == 1

        book = client.get(
            "/api/v1/warehouse-keeper/book",
            params={"warehouse_id": MAIN_WAREHOUSE_ID},
            headers=count_sheet_headers,
        )
        assert book.status_code == 200, book.text
        assert voucher_id in {row["voucher_id"] for row in book.json()["items"]}

        card = client.get(
            "/api/v1/warehouse-keeper/card",
            params={"warehouse_id": MAIN_WAREHOUSE_ID, "item_id": GOODS_ITEM_ID},
            headers=count_sheet_headers,
        )
        assert card.status_code == 200, card.text
        assert card.json()["items"], "thẻ kho phải có dòng sau khi thủ kho ghi sổ"
    finally:
        with unit_of_work(session_factory, scope) as session:
            enable_keeper(session, enabled=False)


def test_keeper_endpoints_refuse_a_role_without_the_keeper_permission(
    client: TestClient, keeper_headers: dict[str, str]
) -> None:
    """Vai kho thường (không có `keeper.warehouse_book.view`) không mở được hàng
    đợi thủ kho — FR-WHK-020 ở tầng HTTP."""
    denied = client.get("/api/v1/warehouse-keeper/queue", headers=keeper_headers)
    assert denied.status_code == 403, denied.text
