"""Tầng HTTP của lát 4D: khóa/mở kỳ + BFF "việc còn thiếu" (bước 14 & 16).

Luật nghiệp vụ đã có test riêng (`test_period_lock_service.py`); ở đây kiểm
những điều chỉ tầng HTTP trả lời được:

* hai mã quyền tách bạch — khóa cần `posting.period.post`, mở cần
  `posting.period.unpost` (`403` khi thiếu — FR-GLE-031);
* mở kỳ thiếu hoặc trống lý do bị `422` ngay ở schema, chưa chạm service;
* lỗi nghiệp vụ mang `error_code` ổn định cho client dựng thông điệp;
* BFF `/vouchers/pending-issues` lọc theo quyền: không có quyền xem loại
  chứng từ → nhóm đó biến mất; không có `posting.balance.view` → phần hàng
  đợi tính lại rỗng.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.recalc_job import BALANCE_VIEW
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_period_lock_service import make_year, period_of

pytestmark = pytest.mark.db

ACTOR_ID = 1

LOCK_PERMISSION = permission_code("posting", "period", Action.POST)
UNLOCK_PERMISSION = permission_code("posting", "period", Action.UNPOST)
GLE_VIEW = permission_code("general_ledger", "journal_voucher", Action.VIEW)


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
def keeper_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Kế toán trưởng: khóa + mở kỳ, xem chứng từ và hàng đợi."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        "ke_toan_khoa_so",
        [LOCK_PERMISSION, UNLOCK_PERMISSION, GLE_VIEW, BALANCE_VIEW],
    )


@pytest.fixture(scope="module")
def lock_only_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Chỉ được khóa — dùng để chứng minh mở kỳ là quyền RIÊNG."""
    return ensure_role(
        session_factory, dataset_alpha, "ke_toan_chi_khoa", [LOCK_PERMISSION, GLE_VIEW]
    )


@pytest.fixture(scope="module")
def clerk_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Không quyền nào của 4D — thấy tab việc-còn-thiếu trống."""
    return ensure_role(session_factory, dataset_alpha, "nhan_vien_thuong", [BALANCE_VIEW])


def _headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role: str,
    prefix: str,
    test_password: str,
    branch_id: int,
) -> dict[str, str]:
    headers = actor(
        client,
        session_factory,
        dataset,
        user_factory,
        role,
        prefix,
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset),
    )
    return {**headers, BRANCH_HEADER: str(branch_id)}


def _fresh_year(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    *,
    code: str,
    start: date,
) -> tuple[int, int]:
    """(year_id, first_period_id) của một niên độ riêng cho một test."""
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        year_id = make_year(session, code=code, start=start)
        return year_id, period_of(session, year_id, 1).id


def _saved_voucher(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    posting_date: date,
) -> str:
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        voucher = JournalVoucherService(session).create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=posting_date,
                posting_date=posting_date,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chờ ghi sổ",
                lines=(
                    JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(9_000)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(9_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        return voucher.voucher_no


class TestPeriodLockApi:
    def test_lock_then_unlock_roundtrip_with_warning_and_permissions(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        keeper_role: str,
        lock_only_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        _year_id, period_id = _fresh_year(
            session_factory, dataset_alpha, context, code="API-LOCK", start=date(2085, 1, 1)
        )
        pending_no = _saved_voucher(session_factory, dataset_alpha, context, date(2085, 1, 12))

        keeper = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            keeper_role,
            "khoaso",
            test_password,
            context.branch_id,
        )
        lock_only = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            lock_only_role,
            "chikhoa",
            test_password,
            context.branch_id,
        )

        lock_key = str(uuid4())
        locked = client.post(
            f"/api/v1/periods/{period_id}/actions/lock",
            headers={**keeper, IDEMPOTENCY_HEADER: lock_key},
        )
        assert locked.status_code == 200, locked.text
        body = locked.json()
        assert body["period"]["locked_at"] is not None
        assert body["warnings"][0]["code"] == "vouchers.unposted"
        assert pending_no in body["warnings"][0]["sample"]

        # Gửi lại CÙNG khóa idempotency: 200, kỳ vẫn khóa, cảnh báo rỗng —
        # khóa lưu tham chiếu chứ không phát lại thân response (RT-12).
        replayed = client.post(
            f"/api/v1/periods/{period_id}/actions/lock",
            headers={**keeper, IDEMPOTENCY_HEADER: lock_key},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["period"]["locked_at"] is not None
        assert replayed.json()["warnings"] == []

        # Khóa lần hai: kỳ đã khóa → lỗi nghiệp vụ có mã ổn định.
        again = client.post(
            f"/api/v1/periods/{period_id}/actions/lock",
            headers={**keeper, IDEMPOTENCY_HEADER: str(uuid4())},
        )
        assert again.status_code == 422
        assert again.json()["error_code"] == "period.closed"

        # Mở kỳ: thiếu lý do → 422 schema; người chỉ-được-khóa → 403.
        no_reason = client.post(
            f"/api/v1/periods/{period_id}/actions/unlock",
            headers={**keeper, IDEMPOTENCY_HEADER: str(uuid4())},
            json={"reason": "   "},
        )
        assert no_reason.status_code == 422
        forbidden = client.post(
            f"/api/v1/periods/{period_id}/actions/unlock",
            headers={**lock_only, IDEMPOTENCY_HEADER: str(uuid4())},
            json={"reason": "thử mở không có quyền"},
        )
        assert forbidden.status_code == 403

        unlocked = client.post(
            f"/api/v1/periods/{period_id}/actions/unlock",
            headers={**keeper, IDEMPOTENCY_HEADER: str(uuid4())},
            json={"reason": "bổ sung chứng từ tháng 1"},
        )
        assert unlocked.status_code == 200, unlocked.text
        assert unlocked.json()["period"]["locked_at"] is None

    def test_out_of_sequence_lock_reports_its_error_code(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        keeper_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            year_id = make_year(session, code="API-SEQ", start=date(2086, 1, 1))
            period_2_id = period_of(session, year_id, 2).id
        keeper = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            keeper_role,
            "khoaseq",
            test_password,
            context.branch_id,
        )
        response = client.post(
            f"/api/v1/periods/{period_2_id}/actions/lock",
            headers={**keeper, IDEMPOTENCY_HEADER: str(uuid4())},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "period.lock_out_of_sequence"


class TestPendingIssuesApi:
    def test_pending_issues_reflect_permissions(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        keeper_role: str,
        clerk_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        pending_no = _saved_voucher(session_factory, dataset_alpha, context, date(2026, 1, 25))

        keeper = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            keeper_role,
            "vieccon",
            test_password,
            context.branch_id,
        )
        response = client.get("/api/v1/vouchers/pending-issues", headers=keeper)
        assert response.status_code == 200, response.text
        body = response.json()
        gle = next(group for group in body["unposted"] if group["document_type"] == "GLE")
        assert gle["count"] >= 1
        assert gle["next_action"] == "post"
        assert any(row["voucher_no"] == pending_no for row in gle["sample"]) or gle["count"] > len(
            gle["sample"]
        )

        # Người không có quyền xem GLE: nhóm biến mất; vẫn thấy hàng đợi
        # tính lại vì có `posting.balance.view`.
        clerk = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            clerk_role,
            "viectrong",
            test_password,
            context.branch_id,
        )
        empty = client.get("/api/v1/vouchers/pending-issues", headers=clerk)
        assert empty.status_code == 200
        assert empty.json()["unposted"] == []

        # Và người này KHÔNG khóa được kỳ — quyền khóa là quyền riêng (403,
        # review 4D M-7: gate quyền phải có test, không chỉ tồn tại).
        forbidden = client.post(
            "/api/v1/periods/1/actions/lock",
            headers={**clerk, IDEMPOTENCY_HEADER: str(uuid4())},
        )
        assert forbidden.status_code == 403

    def test_recalc_queue_section_requires_balance_view(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        lock_only_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        """Có quyền xem GLE nhưng KHÔNG có `posting.balance.view` → phần hàng
        đợi tính lại rỗng dù trong DB đang có dấu bẩn (review 4D, M-7)."""
        with unit_of_work(
            session_factory, posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        ) as session:
            voucher = JournalVoucherService(session).create(
                JournalVoucherIn(
                    branch_id=context.branch_id,
                    document_date=date(2026, 1, 26),
                    posting_date=date(2026, 1, 26),
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    description="tạo dấu bẩn cho test quyền",
                    lines=(
                        JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(7_000)),
                        JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(7_000)),
                    ),
                ),
                user_id=ACTOR_ID,
            )
            JournalVoucherService(session).post(voucher.id, user_id=ACTOR_ID)

        gle_only = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            lock_only_role,
            "glekhongq",
            test_password,
            context.branch_id,
        )
        response = client.get("/api/v1/vouchers/pending-issues", headers=gle_only)
        assert response.status_code == 200, response.text
        assert response.json()["recalc_pending"] == []
