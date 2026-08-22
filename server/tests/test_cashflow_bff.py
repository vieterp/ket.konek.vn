"""BFF "Tiền vào tiền ra" (`/api/v1/cashflow/*`, bước 16 phase-06, lát 6E-1).

Ba nhóm bất biến, và nhóm QUYỀN là nhóm đắt nhất nếu sai:

* hàng thẻ gộp quỹ + từng TK ngân hàng, số dư tính trên đúng phạm vi chi nhánh;
* **quyền đi theo từng nửa**: người chỉ có quyền quỹ không thấy một đồng nào
  của phía ngân hàng, kể cả khi họ gọi thẳng endpoint với `source=bank` (họ
  hàng lỗi leo quyền 6B H-1 — cổng phải nằm trên DỮ LIỆU trả về, không nằm
  trên tên màn hình);
* lưới giao dịch và thẻ số dư đọc cùng một nguồn nên không cãi nhau được.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import Ledger
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.bank.models import BankVoucherKind
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.cash_book.balance_service import cash_balance_as_of, cash_balances_as_of
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
BOTH_ROLE = "cashflow_ca_hai"
CASH_ONLY_ROLE = "cashflow_chi_quy"

# Cửa sổ riêng của tệp này — dữ liệu tệp khác nằm ở tháng 1 trên cùng
# `dataset_alpha` dùng chung (bài học lệ-thuộc-thứ-tự lát 5D: tách bằng THỜI
# GIAN, không bằng thứ tự chạy).
SEP_10 = date(2026, 9, 10)
SEP_11 = date(2026, 9, 11)
BANK_ACCOUNT_CODE = "CF-BFF-001"


def _cash_codes() -> list[str]:
    return [
        permission_code("cash_book", "receipt", Action.VIEW),
        permission_code("cash_book", "payment", Action.VIEW),
    ]


def _bank_codes() -> list[str]:
    return [
        permission_code("bank", name, Action.VIEW)
        for name in ("credit_advice", "payment_order", "cheque", "internal_transfer")
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
    accounts = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    # Gieo nghiệp vụ BC/UNC/SEC vào cùng gói test — hàm này chỉ có tác dụng
    # phụ, bộ TK vẫn là bộ của `seed_cash_book_package_data`.
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return accounts


@pytest.fixture(scope="module")
def bank_account_id(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code=BANK_ACCOUNT_CODE
    )


@pytest.fixture(scope="module")
def seeded(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
) -> dict[str, Decimal]:
    """Một phiếu thu quỹ + một giấy báo có ngân hàng, đều ĐÃ ghi sổ."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    cash_amount = Decimal("640000")
    bank_amount = Decimal("880000")
    with unit_of_work(session_factory, scope) as session:
        cash_service = CashVoucherService(session)
        receipt = cash_service.create(
            CashVoucherIn(
                kind=CashVoucherKind.RECEIPT,
                operation_code="thu-khac",
                cash_account_id=accounts["111"],
                branch_id=context.branch_id,
                document_date=SEP_10,
                posting_date=SEP_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="BFF thu quỹ",
                lines=(
                    CashVoucherLineIn(
                        debit_account_id=accounts["111"],
                        credit_account_id=accounts["3381"],
                        amount_fc=cash_amount,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        cash_service.post(receipt.id, user_id=ACTOR_ID)

        bank_service = BankVoucherService(session)
        advice = bank_service.create(
            BankVoucherIn(
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation_code="thu-khac",
                bank_account_id=bank_account_id,
                branch_id=context.branch_id,
                document_date=SEP_10,
                posting_date=SEP_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="BFF báo có",
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["3381"],
                        amount_fc=bank_amount,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        bank_service.post(advice.id, user_id=ACTOR_ID)
    return {"cash": cash_amount, "bank": bank_amount}


Headers = Callable[[str, str], dict[str, str]]


@pytest.fixture
def headers_for(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> Headers:
    def make(role_code: str, prefix: str) -> dict[str, str]:
        return {
            **actor(
                client,
                session_factory,
                dataset_alpha,
                user_factory,
                role_code,
                prefix,
                test_password,
                branch_codes=[context.branch_code],
            ),
            BRANCH_HEADER: str(context.branch_id),
        }

    return make


@pytest.fixture(scope="module")
def both_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, BOTH_ROLE, [*_cash_codes(), *_bank_codes()])


@pytest.fixture(scope="module")
def cash_only_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, CASH_ONLY_ROLE, _cash_codes())


def test_overview_lists_cash_and_bank_cards(
    client: TestClient,
    headers_for: Headers,
    both_role: str,
    seeded: dict[str, Decimal],
    bank_account_id: int,
) -> None:
    response = client.get(
        "/api/v1/cashflow/overview",
        params={"as_of": SEP_11.isoformat()},
        headers=headers_for(both_role, "cf_both"),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    cash_codes = {card["account_code"] for card in body["cash_accounts"]}
    assert "111" in cash_codes

    bank_cards = {card["bank_account_id"]: card for card in body["bank_accounts"]}
    assert bank_account_id in bank_cards
    card = bank_cards[bank_account_id]
    assert card["bank_account_code"] == BANK_ACCOUNT_CODE
    assert card["bank_name"] == "Ngân hàng test"
    assert Decimal(card["balance"]) == seeded["bank"]


def test_a_cash_only_user_never_sees_bank_money(
    client: TestClient,
    headers_for: Headers,
    cash_only_role: str,
    seeded: dict[str, Decimal],
) -> None:
    """Thiếu quyền ngân hàng thì trang vẫn mở (thủ quỹ phải làm việc được),
    nhưng KHÔNG có một con số nào của phía ngân hàng — kể cả tổng chưa gắn."""
    headers = headers_for(cash_only_role, "cf_cash")
    overview = client.get(
        "/api/v1/cashflow/overview",
        params={"as_of": SEP_11.isoformat()},
        headers=headers,
    )
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["cash_accounts"], "người có quyền quỹ phải thấy thẻ quỹ"
    assert body["bank_accounts"] == []
    assert Decimal(body["unassigned_deposit"]) == 0

    # Và cổng phải nằm trên chính endpoint dữ liệu, không chỉ trên hàng thẻ.
    refused = client.get(
        "/api/v1/cashflow/transactions",
        params={"source": "bank"},
        headers=headers,
    )
    assert refused.status_code == 403, refused.text


def test_a_user_with_neither_permission_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    headers_for: Headers,
) -> None:
    role = ensure_role(session_factory, dataset_alpha, "cashflow_khong_gi", [])
    response = client.get("/api/v1/cashflow/overview", headers=headers_for(role, "cf_none"))
    assert response.status_code == 403, response.text


def test_transactions_carry_the_sign_of_the_selected_account(
    client: TestClient,
    headers_for: Headers,
    both_role: str,
    seeded: dict[str, Decimal],
    accounts: dict[str, int],
    bank_account_id: int,
) -> None:
    headers = headers_for(both_role, "cf_grid")

    cash = client.get(
        "/api/v1/cashflow/transactions",
        params={
            "source": "cash",
            "cash_account_id": accounts["111"],
            "from_date": SEP_10.isoformat(),
            "to_date": SEP_11.isoformat(),
        },
        headers=headers,
    )
    assert cash.status_code == 200, cash.text
    cash_rows = [row for row in cash.json()["items"] if row["description"] == "BFF thu quỹ"]
    assert len(cash_rows) == 1
    # Phiếu THU làm quỹ tăng → dấu dương trên thẻ quỹ.
    assert Decimal(cash_rows[0]["amount"]) == seeded["cash"]
    assert cash_rows[0]["source"] == "cash"

    bank = client.get(
        "/api/v1/cashflow/transactions",
        params={
            "source": "bank",
            "bank_account_id": bank_account_id,
            "from_date": SEP_10.isoformat(),
            "to_date": SEP_11.isoformat(),
        },
        headers=headers,
    )
    assert bank.status_code == 200, bank.text
    bank_rows = bank.json()["items"]
    assert [row["description"] for row in bank_rows] == ["BFF báo có"]
    assert Decimal(bank_rows[0]["amount"]) == seeded["bank"]
    # Lưới ngân hàng không được lẫn phiếu quỹ và ngược lại.
    assert all(row["description"] != "BFF thu quỹ" for row in bank_rows)


def test_transactions_report_a_total_for_the_pager(
    client: TestClient,
    headers_for: Headers,
    both_role: str,
    seeded: dict[str, Decimal],
    accounts: dict[str, int],
) -> None:
    headers = headers_for(both_role, "cf_page")
    params = {
        "source": "cash",
        "cash_account_id": accounts["111"],
        "from_date": SEP_10.isoformat(),
        "to_date": SEP_11.isoformat(),
        "limit": 1,
    }
    response = client.get("/api/v1/cashflow/transactions", params=params, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) <= 1
    # `total` đếm TRƯỚC khi cắt trang, nếu không thanh phân trang vô nghĩa.
    assert body["total"] >= 1


def test_card_balance_agrees_with_the_guard_balance(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    seeded: dict[str, Decimal],
) -> None:
    """`cash_balances_as_of` (hàng thẻ) và `cash_balance_as_of` (guard chi quá
    tồn) phải cho CÙNG con số cho cùng một TK trong cùng một chi nhánh — hai
    đường đọc khác nhau cho cùng một câu hỏi là chỗ số liệu bắt đầu cãi nhau.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        cards = {
            row.account_code: row.balance
            for row in cash_balances_as_of(
                session,
                ledger=int(Ledger.FINANCIAL),
                branch_ids=(context.branch_id,),
                as_of=SEP_11,
            )
        }
        guard = cash_balance_as_of(
            session,
            ledger=int(Ledger.FINANCIAL),
            branch_id=context.branch_id,
            account_code="111",
            as_of=SEP_11,
        )
    assert cards["111"] == guard
