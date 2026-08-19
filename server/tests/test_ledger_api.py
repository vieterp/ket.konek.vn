"""Bảng cân đối tài khoản qua HTTP — hợp đồng API của slice 4B.

Kiểm phần **đi qua HTTP**: quyền `posting.balance.view`, tham số sai trả
`422`, và thân phản hồi mang đúng số + cờ `stale`. Phép toán số dư đã có
`test_trial_balance_query.py` — không kiểm lại ở đây.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.main import create_app
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.recalc_job import BALANCE_VIEW
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)

VIEWER_ROLE = "xem_bang_can_doi"
BLIND_ROLE = "khong_quyen_so_du"


@pytest.fixture
def client(test_settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def viewer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, VIEWER_ROLE, [BALANCE_VIEW])


@pytest.fixture(scope="module")
def blind_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    # Có đăng nhập, không có quyền xem số dư — kiểm 403 chứ không 401.
    return ensure_role(session_factory, dataset_alpha, BLIND_ROLE, ["system.job.view"])


@pytest.fixture
def viewer_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    viewer_role: str,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        viewer_role,
        "balance_viewer",
        test_password,
        branch_codes=[context.branch_code],
    )


@pytest.fixture
def blind_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    blind_role: str,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        blind_role,
        "balance_blind",
        test_password,
        branch_codes=[context.branch_code],
    )


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _post_one(session: Session, context: PostingContext) -> int:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=JAN_15,
            posting_date=JAN_15,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="chứng từ test API sổ cái",
            lines=(
                JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(25_000)),
                JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(25_000)),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.period_id


def test_trial_balance_returns_rows_with_stale_flag(
    client: TestClient,
    run: Runner,
    context: PostingContext,
    viewer_headers: dict[str, str],
) -> None:
    period_id = run(_post_one_wrapper(context))

    response = client.get(
        "/api/v1/ledger/trial-balance",
        params={"period_id": period_id, "ledger": 0},
        headers=viewer_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Vừa ghi sổ, chưa recalc → kỳ bẩn, số tính thẳng và cờ phải nói ra điều đó.
    assert body["stale"] is True
    assert body["period_id"] == period_id
    codes = {row["account_code"]: row for row in body["rows"]}
    assert Decimal(codes["111"]["period_credit"]) == Decimal("25000.00")
    assert Decimal(codes["642"]["period_debit"]) == Decimal("25000.00")


def _post_one_wrapper(context: PostingContext) -> Callable[[Session], object]:
    def work(session: Session) -> object:
        return _post_one(session, context)

    return work


def test_trial_balance_requires_balance_view_permission(
    client: TestClient,
    run: Runner,
    context: PostingContext,
    blind_headers: dict[str, str],
) -> None:
    def work(session: Session) -> object:
        return session.execute(
            select(AccountingPeriod.id)
            .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
            .order_by(AccountingPeriod.period_no)
            .limit(1)
        ).scalar_one()

    period_id = run(work)
    response = client.get(
        "/api/v1/ledger/trial-balance",
        params={"period_id": period_id},
        headers=blind_headers,
    )
    assert response.status_code == 403


def test_trial_balance_rejects_unknown_ledger(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    response = client.get(
        "/api/v1/ledger/trial-balance",
        params={"period_id": 1, "ledger": 7},
        headers=viewer_headers,
    )
    assert response.status_code == 422


def test_trial_balance_unknown_period_is_a_domain_error(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    response = client.get(
        "/api/v1/ledger/trial-balance",
        params={"period_id": 999_999},
        headers=viewer_headers,
    )
    # `PeriodNotFoundError` mặc định 422 (lỗi nghiệp vụ), thân RFC 7807.
    assert response.status_code == 422
    assert response.json()["error_code"] == "period.not_found"
