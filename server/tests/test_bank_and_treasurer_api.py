"""Chứng từ tiền gửi + thủ quỹ qua HTTP (lát 6C — đóng lỗ M-4 review).

Trọng tâm là những gì CHỈ lộ qua tầng HTTP (luật nghiệp vụ đã kiểm ở tệp
service): cổng quyền theo TỪNG loại chứng từ tiền gửi, luật side↔partner_kind
của picker công nợ bank (gương của 6B H-1 — mutation gỡ luật này từng SỐNG),
idempotency của hai POST mới, và FR-WHK-020: vai trò Thủ quỹ (chỉ
`treasurer.cash_book.{view,post}` + quyền XEM phiếu) không sửa/ghi sổ được
chứng từ kế toán.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.config.catalog import TREASURER_ENABLED_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Setting
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

BANK_ROLE = "ke_toan_ngan_hang"
TREASURER_ROLE = "thu_quy"


def _bank_codes() -> list[str]:
    codes: list[str] = []
    for name in ("credit_advice", "payment_order", "cheque", "internal_transfer"):
        codes.extend(
            permission_code("bank", name, action)
            for action in (Action.VIEW, Action.CREATE, Action.EDIT, Action.POST, Action.UNPOST)
        )
    return codes


def _treasurer_codes() -> list[str]:
    """Đúng bộ quyền FR-WHK-020: hàng đợi + sổ của mình, XEM phiếu — không sửa."""
    return [
        permission_code("treasurer", "cash_book", Action.VIEW),
        permission_code("treasurer", "cash_book", Action.POST),
        permission_code("cash_book", "receipt", Action.VIEW),
        permission_code("cash_book", "payment", Action.VIEW),
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
    seeded = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return seeded


@pytest.fixture(scope="module")
def vnd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code="0031-BANK-HTTP"
    )


@pytest.fixture
def banker_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    role = ensure_role(session_factory, dataset_alpha, BANK_ROLE, _bank_codes())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "bank_author",
        test_password,
        branch_codes=[context.branch_code],
    )


@pytest.fixture
def treasurer_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    role = ensure_role(session_factory, dataset_alpha, TREASURER_ROLE, _treasurer_codes())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "treasurer_user",
        test_password,
        branch_codes=[context.branch_code],
    )


def _credit_advice_body(
    context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> dict[str, Any]:
    return {
        "kind": 0,
        "operation_code": "thu-khac",
        "bank_account_id": vnd_account,
        "branch_id": context.branch_id,
        "document_date": "2026-02-12",
        "posting_date": "2026-02-12",
        "currency_code": "VND",
        "exchange_rate": "1",
        "description": "báo có qua HTTP",
        "lines": [
            {
                "debit_account_id": accounts["112"],
                "credit_account_id": accounts["3381"],
                "amount_fc": "90000",
            }
        ],
    }


def test_bank_create_is_idempotent_and_permission_is_per_kind(
    client: TestClient,
    banker_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
    vnd_account: int,
) -> None:
    key = uuid4().hex
    body = _credit_advice_body(context, accounts, vnd_account)
    first = client.post(
        "/api/v1/bank/vouchers", json=body, headers={**banker_headers, IDEMPOTENCY_HEADER: key}
    )
    assert first.status_code == 201, first.text
    assert first.json()["voucher_no"].startswith("BC26-")

    replay = client.post(
        "/api/v1/bank/vouchers", json=body, headers={**banker_headers, IDEMPOTENCY_HEADER: key}
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]

    # Vai trò chỉ có quyền báo có KHÔNG tạo được ủy nhiệm chi — mã quyền theo
    # từng loại chứng từ, không phải một quyền "bank" chung.
    credit_only = ensure_role(
        session_factory,
        dataset_alpha,
        "chi_bao_co",
        [
            permission_code("bank", "credit_advice", action)
            for action in (Action.VIEW, Action.CREATE)
        ],
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        credit_only,
        "bank_credit_only",
        test_password,
        branch_codes=[context.branch_code],
    )
    unc_body = {
        **_credit_advice_body(context, accounts, vnd_account),
        "kind": 1,
        "operation_code": "chi-khac",
        "lines": [
            {
                "debit_account_id": accounts["642"],
                "credit_account_id": accounts["112"],
                "amount_fc": "10000",
            }
        ],
    }
    refused = client.post(
        "/api/v1/bank/vouchers",
        json=unc_body,
        headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert refused.status_code == 403


def test_bank_open_invoices_side_must_match_partner_kind_and_requires_view(
    client: TestClient,
    banker_headers: dict[str, str],
    treasurer_headers: dict[str, str],
    context: PostingContext,
) -> None:
    """Gương của 6B H-1 trên router bank: cặp side↔partner_kind lệch chiều bị
    chặn 422 kể cả với người đủ quyền hai chiều; thiếu quyền xem → 403."""
    mismatched = client.get(
        "/api/v1/bank/open-invoices",
        params={
            "side": "receivable",
            "partner_kind": 1,
            "partner_id": 1,
            "branch_id": context.branch_id,
            "as_of": "2026-02-12",
        },
        headers=banker_headers,
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error_code"] == "settlement.side_mismatch"

    refused = client.get(
        "/api/v1/bank/open-invoices",
        params={
            "side": "receivable",
            "partner_kind": 0,
            "partner_id": 1,
            "branch_id": context.branch_id,
            "as_of": "2026-02-12",
        },
        headers=treasurer_headers,
    )
    assert refused.status_code == 403


def _set_treasurer_enabled(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    enabled: bool,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        value = "true" if enabled else "false"
        row = session.scalar(
            select(Setting).where(Setting.key == TREASURER_ENABLED_KEY, Setting.scope == "system")
        )
        if row is None:
            session.add(
                Setting(
                    scope="system", key=TREASURER_ENABLED_KEY, value=value, value_type="boolean"
                )
            )
        else:
            row.value = value


def test_treasurer_role_books_queue_but_cannot_touch_accounting_vouchers(
    client: TestClient,
    treasurer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """FR-WHK-020/BR-WHK-04 qua HTTP: thủ quỹ ghi sổ quỹ được nhưng mọi đường
    sửa/ghi sổ/xóa chứng từ kế toán đều 403 — bằng thiết kế bộ quyền."""
    cashier_role = ensure_role(
        session_factory,
        dataset_alpha,
        "ke_toan_quy_6c",
        [
            permission_code("cash_book", "receipt", action)
            for action in (Action.VIEW, Action.CREATE, Action.EDIT, Action.POST)
        ],
    )
    cashier = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        cashier_role,
        "cash_author_6c",
        test_password,
        branch_codes=[context.branch_code],
    )
    receipt_body = {
        "kind": 0,
        "operation_code": "thu-khac",
        "cash_account_id": accounts["111"],
        "branch_id": context.branch_id,
        "document_date": "2026-02-12",
        "posting_date": "2026-02-12",
        "currency_code": "VND",
        "exchange_rate": "1",
        "lines": [
            {
                "debit_account_id": accounts["111"],
                "credit_account_id": accounts["3381"],
                "amount_fc": "77000",
            }
        ],
    }
    try:
        _set_treasurer_enabled(session_factory, dataset_alpha, context, True)
        created = client.post(
            "/api/v1/cash-book/vouchers",
            json=receipt_body,
            headers={**cashier, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert created.status_code == 201, created.text
        voucher_id = created.json()["id"]
        posted = client.post(
            f"/api/v1/vouchers/{voucher_id}/actions/post",
            headers={**cashier, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert posted.status_code == 200, posted.text

        # Thủ quỹ KHÔNG sửa được, KHÔNG bỏ ghi sổ được chứng từ kế toán.
        forbidden_edit = client.put(
            f"/api/v1/cash-book/vouchers/{voucher_id}",
            json={**receipt_body, "row_version": 1},
            headers=treasurer_headers,
        )
        assert forbidden_edit.status_code == 403
        forbidden_unpost = client.post(
            f"/api/v1/vouchers/{voucher_id}/actions/unpost",
            headers={**treasurer_headers, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert forbidden_unpost.status_code == 403

        # Kế toán (không có treasurer.cash_book.post) không ghi sổ quỹ được.
        forbidden_book = client.post(
            "/api/v1/treasurer/queue/actions/book",
            json={"voucher_ids": [voucher_id], "book_date_mode": "posting_date"},
            headers={**cashier, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert forbidden_book.status_code == 403

        queue = client.get("/api/v1/treasurer/queue", headers=treasurer_headers)
        assert queue.status_code == 200
        assert voucher_id in {row["voucher_id"] for row in queue.json()["items"]}

        key = uuid4().hex
        booked = client.post(
            "/api/v1/treasurer/queue/actions/book",
            json={"voucher_ids": [voucher_id], "book_date_mode": "posting_date"},
            headers={**treasurer_headers, IDEMPOTENCY_HEADER: key},
        )
        assert booked.status_code == 200, booked.text
        assert booked.json()["booked_count"] == 1

        replay = client.post(
            "/api/v1/treasurer/queue/actions/book",
            json={"voucher_ids": [voucher_id], "book_date_mode": "posting_date"},
            headers={**treasurer_headers, IDEMPOTENCY_HEADER: key},
        )
        assert replay.status_code == 200
        assert replay.json()["booked_count"] == 1

        book = client.get("/api/v1/treasurer/cash-book", headers=treasurer_headers)
        assert book.status_code == 200
        assert voucher_id in {row["voucher_id"] for row in book.json()["items"]}
    finally:
        _set_treasurer_enabled(session_factory, dataset_alpha, context, False)
