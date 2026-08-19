"""Tầng HTTP của hai endpoint đọc mới lát 4E.

* `GET /api/v1/accounts` — tra TK cho form chứng từ: gói chọn theo (chế độ kế
  toán của năm, ngày); `search` khớp tiền tố số hiệu hoặc chứa trong tên;
  `detail_tracking` phải có mặt trong phản hồi (form hiện cột chiều theo nó);
  quyền `master.account.view` là cổng.
* `GET /api/v1/ledger/postings` — danh sách phát sinh: chỉ chứng từ đã ghi sổ
  xuất hiện; lọc theo TK + kỳ là đường drill-down; quyền `posting.balance.view`
  là cổng (cùng cổng bảng cân đối).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.api.routers.accounts import ACCOUNT_VIEW
from ket.kernel.config.accounts_models import ChartOfAccount
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
MAR_1 = date(2026, 3, 1)


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
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Được tra TK và xem phát sinh — người lập chứng từ điển hình."""
    return ensure_role(
        session_factory, dataset_alpha, "ke_toan_tra_cuu", [ACCOUNT_VIEW, BALANCE_VIEW]
    )


@pytest.fixture(scope="module")
def outsider_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Không quyền đọc nào của 4E — mọi endpoint mới phải trả 403."""
    return ensure_role(session_factory, dataset_alpha, "vai_tro_ngoai_cuoc", [])


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


class TestAccountsLookup:
    def test_search_by_code_prefix_returns_the_subtree_with_tracking_info(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "tra-tk",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/accounts",
            params={"on_date": MAR_1.isoformat(), "search": "131"},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["package_id"] == context.package_id
        codes = {item["code"]: item for item in body["items"]}
        assert "131" in codes
        # `detail_tracking` là lý do endpoint tồn tại: form đọc nó để hiện
        # cột chiều bắt buộc của TK đã chọn (FR-SYS-021).
        assert codes["131"]["detail_tracking"] == ["customer"]

    def test_search_by_name_and_summary_flag(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "tra-ten",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/accounts",
            params={"on_date": MAR_1.isoformat(), "search": "11"},
            headers=headers,
        )
        assert response.status_code == 200
        items = {item["code"]: item for item in response.json()["items"]}
        # "11" là TK tổng hợp trong seed — trả về để vẽ cây nhưng cờ phải bật
        # cho form chặn chọn (BR-SYS-03).
        assert items["11"]["is_summary"] is True
        assert items["111"]["is_summary"] is False

    def test_a_date_outside_any_fiscal_year_is_a_domain_error(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "tra-1990",
            test_password,
            context.branch_id,
        )
        response = client.get("/api/v1/accounts", params={"on_date": "1990-01-01"}, headers=headers)
        assert response.status_code == 422
        assert response.json()["error_code"] == "domain_error"

    def test_inactive_accounts_hide_from_search_but_hydrate_by_ids(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        """Hai mặt của cùng luật: TK ngừng dùng không được CHỌN THÊM (search ẩn
        nó), nhưng chứng từ cũ trỏ vào nó vẫn phải DỰNG LẠI được (ids trả nó)."""
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            row = session.execute(
                select(ChartOfAccount)
                .where(ChartOfAccount.package_id == context.package_id)
                .where(ChartOfAccount.code == "9991")
            ).scalar_one_or_none()
            if row is None:
                row = ChartOfAccount(
                    package_id=context.package_id,
                    code="9991",
                    name="TK ngừng dùng (probe)",
                    path="0.",
                    balance_nature=1,
                    is_summary=False,
                    is_inactive=True,
                )
                session.add(row)
                session.flush()
                row.path = f"{row.id}."
            inactive_id = row.id

        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "tra-ngung",
            test_password,
            context.branch_id,
        )
        searched = client.get(
            "/api/v1/accounts",
            params={"on_date": MAR_1.isoformat(), "search": "9991"},
            headers=headers,
        )
        assert searched.status_code == 200
        assert searched.json()["items"] == []

        hydrated = client.get(
            "/api/v1/accounts",
            params={"on_date": MAR_1.isoformat(), "ids": [inactive_id]},
            headers=headers,
        )
        assert hydrated.status_code == 200
        codes = [item["code"] for item in hydrated.json()["items"]]
        assert codes == ["9991"]

    def test_without_the_view_permission_the_lookup_is_403(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        outsider_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            outsider_role,
            "tra-cam",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/accounts", params={"on_date": MAR_1.isoformat()}, headers=headers
        )
        assert response.status_code == 403


class TestLedgerPostings:
    def _post_one(
        self,
        session_factory: sessionmaker[Session],
        dataset: DatasetRef,
        context: PostingContext,
    ) -> tuple[str, int]:
        """Ghi sổ một chứng từ 2 dòng; trả về (voucher_no, period_id của 3/2026)."""
        scope = posting_scope(dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = JournalVoucherService(session)
            voucher = service.create(
                JournalVoucherIn(
                    branch_id=context.branch_id,
                    document_date=MAR_1,
                    posting_date=MAR_1,
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    description="phát sinh cho danh sách sổ cái",
                    lines=(
                        JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(70_000)),
                        JournalLineIn(
                            account_id=context.accounts["111"], credit_fc=Decimal(70_000)
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)
            period_id = session.execute(
                select(AccountingPeriod.id)
                .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
                .where(AccountingPeriod.start_date <= MAR_1)
                .where(AccountingPeriod.end_date >= MAR_1)
            ).scalar_one()
            return voucher.voucher_no, period_id

    def test_posted_lines_show_up_filtered_by_account_and_period(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        voucher_no, period_id = self._post_one(session_factory, dataset_alpha, context)
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "doc-ps",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/ledger/postings",
            params={
                "account_id": context.accounts["642"],
                "period_id": period_id,
                "branch_id": context.branch_id,
            },
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        row = body["items"][0]
        assert row["voucher_no"] == voucher_no
        assert row["account_code"] == "642"
        assert Decimal(row["debit"]) == Decimal("70000.00")

    def test_rows_come_back_oldest_first_regardless_of_insert_order(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        reader_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        """Thứ tự đọc sổ là (ngày, số CT, dòng, id) — cổng canh cho ORDER BY
        (mutation review 4E: bỏ ORDER BY phải làm test này đỏ)."""
        apr_20, apr_10 = date(2026, 4, 20), date(2026, 4, 10)
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = JournalVoucherService(session)
            # Ghi sổ ngày MUỘN trước, ngày SỚM sau — thứ tự chèn ngược thứ tự đọc.
            for posting_date in (apr_20, apr_10):
                voucher = service.create(
                    JournalVoucherIn(
                        branch_id=context.branch_id,
                        document_date=posting_date,
                        posting_date=posting_date,
                        currency_code="VND",
                        exchange_rate=Decimal(1),
                        description="thứ tự đọc sổ",
                        lines=(
                            JournalLineIn(
                                account_id=context.accounts["511"], credit_fc=Decimal(10_000)
                            ),
                            JournalLineIn(
                                account_id=context.accounts["111"], debit_fc=Decimal(10_000)
                            ),
                        ),
                    ),
                    user_id=ACTOR_ID,
                )
                service.post(voucher.id, user_id=ACTOR_ID)
            april_period_id = session.execute(
                select(AccountingPeriod.id)
                .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
                .where(AccountingPeriod.start_date <= apr_10)
                .where(AccountingPeriod.end_date >= apr_20)
            ).scalar_one()

        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            reader_role,
            "doc-thu-tu",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/ledger/postings",
            params={
                "account_id": context.accounts["511"],
                "period_id": april_period_id,
                "branch_id": context.branch_id,
            },
            headers=headers,
        )
        assert response.status_code == 200
        dates = [row["posting_date"] for row in response.json()["items"]]
        assert dates == sorted(dates)
        assert dates[0] == apr_10.isoformat()

    def test_without_balance_view_the_listing_is_403(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        outsider_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            outsider_role,
            "doc-cam",
            test_password,
            context.branch_id,
        )
        response = client.get("/api/v1/ledger/postings", headers=headers)
        assert response.status_code == 403


class TestFiscalYears:
    """`GET /api/v1/fiscal-years` — chỉ cần đăng nhập (quyết định 4E: lịch năm/kỳ
    không mang số liệu), nhưng KHÔNG được mở cho người chưa đăng nhập."""

    def test_years_come_with_their_periods_sorted(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        outsider_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        # Vai trò KHÔNG có quyền nào — Authorized-only nghĩa là vẫn xem được lịch.
        headers = _headers(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            outsider_role,
            "xem-lich",
            test_password,
            context.branch_id,
        )
        response = client.get("/api/v1/fiscal-years", headers=headers)
        assert response.status_code == 200
        years = response.json()["items"]
        seeded = next(year for year in years if year["id"] == context.fiscal_year_id)
        period_nos = [period["period_no"] for period in seeded["periods"]]
        assert period_nos == sorted(period_nos)
        assert len(period_nos) == 12
        # Năm mới nhất đứng trước — bộ chọn năm mặc định vào năm đang làm việc.
        starts = [year["start_date"] for year in years]
        assert starts == sorted(starts, reverse=True)

    def test_without_a_session_the_calendar_is_unauthorized(self, client: TestClient) -> None:
        response = client.get("/api/v1/fiscal-years")
        assert response.status_code == 401
