"""Phiếu thu/chi + kiểm kê quỹ qua HTTP (lát 6B).

Trọng tâm là những gì CHỈ lộ qua tầng HTTP: hook vòng đời của registry chạy
trên endpoint hành động dùng chung (`/vouchers/{id}/actions/*` cộng/gỡ đối
trừ, `DELETE /vouchers/{id}` trả bộ đếm tham chiếu), phân quyền tách PT/PC,
picker công nợ, và biên bản kiểm kê. Luật nghiệp vụ đã kiểm ở các tệp service.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data, seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.posting.opening_balances.models import OpeningBalanceInvoice
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

CASHIER_ROLE = "ke_toan_quy"
CUSTOMER_ID = 9301


def _cash_codes() -> list[str]:
    codes: list[str] = []
    for name in ("receipt", "payment"):
        codes.extend(
            permission_code("cash_book", name, action)
            for action in (
                Action.VIEW,
                Action.CREATE,
                Action.EDIT,
                Action.DELETE,
                Action.POST,
                Action.UNPOST,
            )
        )
    codes.extend(
        permission_code("cash_book", "count_sheet", action)
        for action in (Action.VIEW, Action.CREATE, Action.DELETE)
    )
    return codes


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
    return seed_cash_book_package_data(session_factory, dataset_alpha, context)


@pytest.fixture(scope="module")
def cashier_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, CASHIER_ROLE, _cash_codes())


@pytest.fixture
def cashier_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    cashier_role: str,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        cashier_role,
        "cash_author",
        test_password,
        branch_codes=[context.branch_code],
    )


def _receipt_body(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    amount: str = "250000",
    settlements: list[dict[str, Any]] | None = None,
    partner: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": 0,
        "operation_code": "thu-no-khach-hang" if partner else "thu-khac",
        "cash_account_id": accounts["111"],
        "branch_id": context.branch_id,
        "document_date": "2026-02-10",
        "posting_date": "2026-02-10",
        "currency_code": "VND",
        "exchange_rate": "1",
        "description": "phiếu thu qua HTTP",
        "lines": [
            {
                "debit_account_id": accounts["111"],
                "credit_account_id": accounts["131"],
                "amount_fc": amount,
                **({"partner_kind": 0, "partner_id": CUSTOMER_ID} if partner else {}),
            }
        ],
    }
    if partner:
        body["partner_kind"] = 0
        body["partner_id"] = CUSTOMER_ID
    if settlements is not None:
        body["settlements"] = settlements
    return body


def _create(
    client: TestClient,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/api/v1/cash-book/vouchers",
        json=body,
        headers={**headers, IDEMPOTENCY_HEADER: key or uuid4().hex},
    )


def test_create_is_idempotent_and_numbers_receipts(
    client: TestClient,
    cashier_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    key = uuid4().hex
    first = _create(client, cashier_headers, _receipt_body(context, accounts), key=key)
    assert first.status_code == 201, first.text
    assert first.json()["voucher_no"].startswith("PT26-")
    assert first.json()["treasurer_status"] == 0

    replay = _create(client, cashier_headers, _receipt_body(context, accounts), key=key)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_shared_action_endpoint_applies_and_reverts_settlements(
    client: TestClient,
    cashier_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Hook `after_post`/`after_unpost` của registry chạy trên endpoint hành
    động dùng chung — đường mà client thật dùng để ghi sổ."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER_ID,
        amount_fc=Decimal("600000"),
        invoice_no="HD-HTTP-1",
    )
    created = _create(
        client,
        cashier_headers,
        _receipt_body(
            context,
            accounts,
            amount="200000",
            partner=True,
            settlements=[{"target_kind": 2, "target_id": str(invoice_id), "amount_fc": "200000"}],
        ),
    )
    assert created.status_code == 201, created.text
    voucher_id = created.json()["id"]
    assert len(created.json()["settlements"]) == 1

    posted = client.post(
        f"/api/v1/vouchers/{voucher_id}/actions/post",
        headers={**cashier_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert posted.status_code == 200, posted.text

    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None and invoice.paid_amount_fc == Decimal("200000")

    listed = client.get(
        "/api/v1/cash-book/open-invoices",
        params={
            "side": "receivable",
            "partner_kind": 0,
            "partner_id": CUSTOMER_ID,
            "branch_id": context.branch_id,
            "as_of": "2026-02-10",
        },
        headers=cashier_headers,
    )
    assert listed.status_code == 200, listed.text
    remaining = {item["target_id"]: item["remaining_fc"] for item in listed.json()["items"]}
    assert Decimal(remaining[str(invoice_id)]) == Decimal("400000")

    unposted = client.post(
        f"/api/v1/vouchers/{voucher_id}/actions/unpost",
        headers={**cashier_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert unposted.status_code == 200, unposted.text
    with unit_of_work(session_factory, scope) as session:
        invoice = session.get(OpeningBalanceInvoice, invoice_id)
        assert invoice is not None and invoice.paid_amount_fc == Decimal(0)


def test_shared_delete_releases_usage_counters(
    client: TestClient,
    cashier_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Hook `before_delete` chạy trên `DELETE /vouchers/{id}` — không có nó bộ
    đếm kẹt và danh mục không bao giờ xóa được."""
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER_ID,
        amount_fc=Decimal("300000"),
        invoice_no="HD-HTTP-2",
    )
    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        before = usage_count_of(session, entity_type="partners", entity_id=CUSTOMER_ID)

    created = _create(
        client,
        cashier_headers,
        _receipt_body(
            context,
            accounts,
            amount="300000",
            partner=True,
            settlements=[{"target_kind": 2, "target_id": str(invoice_id), "amount_fc": "300000"}],
        ),
    )
    assert created.status_code == 201, created.text
    voucher_id = created.json()["id"]

    with unit_of_work(session_factory, scope) as session:
        assert usage_count_of(session, entity_type="partners", entity_id=CUSTOMER_ID) == before + 2

    deleted = client.delete(f"/api/v1/vouchers/{voucher_id}", headers=cashier_headers)
    assert deleted.status_code == 204, deleted.text
    with unit_of_work(session_factory, scope) as session:
        assert usage_count_of(session, entity_type="partners", entity_id=CUSTOMER_ID) == before


def test_count_sheet_endpoints_round_trip(
    client: TestClient,
    cashier_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    created = client.post(
        "/api/v1/cash-book/count-sheets",
        json={
            "branch_id": context.branch_id,
            "cash_account_id": accounts["111"],
            "count_date": "2026-02-15",
            "counted_total": "50000",
            "lines": [{"denomination": "50000", "quantity": 1}],
        },
        headers={**cashier_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert Decimal(sheet["difference"]) == Decimal(sheet["counted_total"]) - Decimal(
        sheet["book_balance"]
    )

    adjusted = client.post(
        f"/api/v1/cash-book/count-sheets/{sheet['id']}/actions/create-adjustment",
        headers={**cashier_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["adjustment_voucher_id"] is not None

    listing = client.get(
        "/api/v1/cash-book/count-sheets",
        params={"cash_account_id": accounts["111"]},
        headers=cashier_headers,
    )
    assert listing.status_code == 200
    assert any(item["id"] == sheet["id"] for item in listing.json()["items"])

    refused_delete = client.delete(
        f"/api/v1/cash-book/count-sheets/{sheet['id']}", headers=cashier_headers
    )
    assert refused_delete.status_code == 409  # đã sinh phiếu xử lý


def test_receipt_permission_does_not_grant_payments(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    receipt_only = ensure_role(
        session_factory,
        dataset_alpha,
        "chi_phieu_thu",
        [
            permission_code("cash_book", "receipt", action)
            for action in (Action.VIEW, Action.CREATE)
        ],
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        receipt_only,
        "cash_receipt_only",
        test_password,
        branch_codes=[context.branch_code],
    )
    allowed = _create(client, headers, _receipt_body(context, accounts))
    assert allowed.status_code == 201, allowed.text

    payment_body = {
        **_receipt_body(context, accounts),
        "kind": 1,
        "operation_code": "chi-khac",
        "lines": [
            {
                "debit_account_id": accounts["642"],
                "credit_account_id": accounts["111"],
                "amount_fc": "10000",
            }
        ],
    }
    refused = _create(client, headers, payment_body)
    assert refused.status_code == 403

    refused_invoices = client.get(
        "/api/v1/cash-book/open-invoices",
        params={
            "side": "payable",
            "partner_kind": 1,
            "partner_id": 1,
            "branch_id": context.branch_id,
            "as_of": "2026-02-10",
        },
        headers=headers,
    )
    assert refused_invoices.status_code == 403


def test_update_via_http_respects_row_version(
    client: TestClient,
    cashier_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    created = _create(client, cashier_headers, _receipt_body(context, accounts)).json()
    stale = client.put(
        f"/api/v1/cash-book/vouchers/{created['id']}",
        json={**_receipt_body(context, accounts), "row_version": created["row_version"] + 5},
        headers=cashier_headers,
    )
    assert stale.status_code == 409

    updated = client.put(
        f"/api/v1/cash-book/vouchers/{created['id']}",
        json={
            **_receipt_body(context, accounts, amount="99000"),
            "row_version": created["row_version"],
        },
        headers=cashier_headers,
    )
    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["lines"][0]["amount_fc"]) == Decimal("99000")


def test_open_invoices_side_must_match_partner_kind(
    client: TestClient,
    cashier_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Sửa H-1 review 6B: cổng quyền kiểm theo `side`, nên cặp side↔partner_kind
    lệch chiều (xin phải-thu của một NCC) là đường vòng và bị chặn 422 — kể cả
    với người có ĐỦ quyền hai chiều."""
    mismatched = client.get(
        "/api/v1/cash-book/open-invoices",
        params={
            "side": "receivable",
            "partner_kind": 1,
            "partner_id": 1,
            "branch_id": context.branch_id,
            "as_of": "2026-02-10",
        },
        headers=cashier_headers,
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error_code"] == "settlement.side_mismatch"
