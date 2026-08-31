"""Bước 20 phase-05: hai dữ liệu kế toán TT99 và TT133, cùng bộ chứng từ tương
đương → bộ sổ + BCTC ra đúng mẫu của từng thông tư, **0 dòng code khác biệt**.

"0 dòng code khác biệt" là cấu trúc của chính tệp này: mọi test tham số hóa
theo scheme và chạy MỘT thân duy nhất — khác biệt duy nhất giữa hai chế độ nằm
trong `CASES` (dữ liệu: mã mẫu, mã TK lá), đúng như khác biệt giữa hai thông tư
nằm trong gói cấu hình chứ không trong engine (LD-06, FR-NFR-055).

Khác `test_reports_api`/`test_statement_builder_api`, bối cảnh ở đây dùng
**gói builtin** (`seed_posting_context_on_builtin`): bài kiểm phải chứng minh hệ
TK + layout + bộ sổ ĐÓNG GÓI của từng thông tư phục vụ đúng, không phải một gói
test tối giản. Hệ quả nhìn thấy được: hai chart khác hình dạng thật (TT99 có
`111` là TK lá, TT133 phải ghi vào `1111`) — "bộ chứng từ tương đương" nghĩa là
cùng nghiệp vụ cùng số tiền, trên TK lá tương ứng của từng chế độ.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.periods.models import AccountingPeriod, AccountingScheme
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.reporting.engine import REPORT_EXPORT, REPORT_VIEW
from ket.reporting.statements import STATEMENT_VIEW
from ket.settings import Settings
from posting_support import (
    PostingContext,
    posting_scope,
    seed_posting_context_on_builtin,
)

pytestmark = pytest.mark.db

JOURNAL_VIEW = permission_code("general_ledger", "journal_voucher", Action.VIEW)
"""Bộ sổ tổng hợp đóng cổng quyền phân hệ từ lát 6G-1 — `reporting.report.view`
một mình không mở được S03a/S03b/S38/S06/S19 nữa."""

ACTOR_ID = 1
VND = "VND"
RANGE = {"from_date": "2026-01-01", "to_date": "2026-12-31"}

REVENUE = 1_000_000
EXPENSE = 200_000


@dataclass(frozen=True)
class SchemeCase:
    """Toàn bộ khác biệt giữa hai chế độ — dữ liệu, không phải nhánh code."""

    dataset_code: str
    scheme: AccountingScheme
    cash_account: str
    revenue_account: str
    expense_account: str
    ledger_book_code: str
    balance_layout: str
    income_layout: str
    foreign_book_codes: frozenset[str]


CASES: dict[str, SchemeCase] = {
    "TT99": SchemeCase(
        dataset_code="sw99",
        scheme=AccountingScheme.TT99,
        cash_account="111",  # TT99: 111 là TK lá (Phụ lục II không mở 1111)
        revenue_account="511",
        expense_account="6421",
        ledger_book_code="S03b-DN",
        balance_layout="B01-DN",
        income_layout="B02-DN",
        foreign_book_codes=frozenset({"S03a-DNN", "S03b-DNN", "S19-DNN", "F01-DNN"}),
    ),
    "TT133": SchemeCase(
        dataset_code="sw133",
        scheme=AccountingScheme.TT133,
        cash_account="1111",  # TT133: 111 là TK tổng hợp, lá là 1111
        revenue_account="5111",
        expense_account="6421",
        ledger_book_code="S03b-DNN",
        balance_layout="B01a-DNN",
        income_layout="B02-DNN",
        foreign_book_codes=frozenset({"S03a-DN", "S03b-DN", "S38-DN", "S06-DN"}),
    ),
}


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


_contexts: dict[str, tuple[DatasetRef, PostingContext]] = {}


@pytest.fixture(scope="module", params=sorted(CASES))
def scheme_key(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def scheme_world(
    scheme_key: str,
    owner_engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[tuple[SchemeCase, DatasetRef, PostingContext]]:
    """Dataset + bối cảnh + bộ chứng từ tương đương của MỘT chế độ.

    Gieo đúng một lần cho mỗi scheme (fixture module-scope tham số hóa): thu
    `REVENUE` tiền mặt (cash/revenue) tháng 1, chi `EXPENSE` (expense/cash)
    tháng 2 — cùng nghiệp vụ, cùng số tiền, TK lá theo chart của chế độ.
    """
    case = CASES[scheme_key]
    if scheme_key not in _contexts:
        ref = provision_dataset(
            owner_engine,
            code=case.dataset_code,
            name=f"Scheme switch {scheme_key}",
            scheme=case.scheme.value,
        )
        context = seed_posting_context_on_builtin(
            session_factory,
            ref,
            scheme=case.scheme,
            account_codes=(case.cash_account, case.revenue_account, case.expense_account),
        )
        _seed_equivalent_books(session_factory, ref, context, case)
        _contexts[scheme_key] = (ref, context)
    ref, context = _contexts[scheme_key]
    yield case, ref, context
    if scheme_key in _contexts:
        del _contexts[scheme_key]
        drop_dataset_schema(owner_engine, case.dataset_code)


def _seed_equivalent_books(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    case: SchemeCase,
) -> None:
    with unit_of_work(
        session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
    ) as session:
        service = JournalVoucherService(session)
        for posting_date, lines in (
            (
                date(2026, 1, 10),
                (
                    (case.cash_account, REVENUE, 0),
                    (case.revenue_account, 0, REVENUE),
                ),
            ),
            (
                date(2026, 2, 5),
                (
                    (case.expense_account, EXPENSE, 0),
                    (case.cash_account, 0, EXPENSE),
                ),
            ),
        ):
            voucher = service.create(
                JournalVoucherIn(
                    branch_id=context.branch_id,
                    document_date=posting_date,
                    posting_date=posting_date,
                    currency_code=VND,
                    exchange_rate=Decimal(1),
                    description="chứng từ tương đương hai chế độ",
                    lines=tuple(
                        JournalLineIn(
                            account_id=context.accounts[account],
                            debit_fc=Decimal(debit),
                            credit_fc=Decimal(credit),
                        )
                        for account, debit, credit in lines
                    ),
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)


def _headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    user_factory: UserFactory,
    password: str,
) -> dict[str, str]:
    role = ensure_role(
        session_factory,
        dataset,
        "doi_chieu_che_do",
        [REPORT_VIEW, REPORT_EXPORT, STATEMENT_VIEW, JOURNAL_VIEW],
    )
    headers = actor(
        client,
        session_factory,
        dataset,
        user_factory,
        role,
        f"sw-{dataset.code}",
        password,
        branch_codes=all_branch_codes(session_factory, dataset),
    )
    return {**headers, BRANCH_HEADER: str(context.branch_id)}


def _period_id(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    month: int,
) -> int:
    with unit_of_work(
        session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
    ) as session:
        period_id = session.scalar(
            select(AccountingPeriod.id).where(
                AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
                AccountingPeriod.period_no == month,
            )
        )
        assert period_id is not None
        return period_id


def _grand_total(content: bytes) -> tuple[Decimal, ...]:
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    sheet = workbook.active
    assert sheet is not None
    for row in sheet.iter_rows(values_only=True):
        if row and row[0] == "Tổng cộng":
            return tuple(
                Decimal(str(value)) for value in row if isinstance(value, (int, float, Decimal))
            )
    raise AssertionError("không thấy dòng Tổng cộng trong tệp xuất")


class TestSchemeSwitch:
    def test_catalog_serves_only_own_scheme_book_codes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        scheme_world: tuple[SchemeCase, DatasetRef, PostingContext],
        user_factory: UserFactory,
        test_password: str,
    ) -> None:
        case, dataset, context = scheme_world
        headers = _headers(client, session_factory, dataset, context, user_factory, test_password)
        listing = client.get("/api/v1/reports", headers=headers)
        assert listing.status_code == 200, listing.text
        codes = {item["code"] for item in listing.json()["reports"]}
        assert case.ledger_book_code in codes
        assert not codes & case.foreign_book_codes

    def test_foreign_scheme_book_is_absent_like_it_never_existed(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        scheme_world: tuple[SchemeCase, DatasetRef, PostingContext],
        user_factory: UserFactory,
        test_password: str,
    ) -> None:
        case, dataset, context = scheme_world
        headers = _headers(client, session_factory, dataset, context, user_factory, test_password)
        for foreign in sorted(case.foreign_book_codes):
            response = client.post(
                f"/api/v1/reports/{foreign}/render",
                json={"format": "xlsx", "params": RANGE},
                headers=headers,
            )
            assert response.status_code == 404, (foreign, response.text)

    def test_ledger_book_renders_under_scheme_specific_form_code(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        scheme_world: tuple[SchemeCase, DatasetRef, PostingContext],
        user_factory: UserFactory,
        test_password: str,
    ) -> None:
        """Cùng bộ chứng từ → Sổ Cái của từng chế độ mang MÃ MẪU riêng nhưng
        cùng con số (khẳng định số tuyệt đối để hai lượt chạy so được với
        nhau mà không cần chạy cùng phiên)."""
        case, dataset, context = scheme_world
        headers = _headers(client, session_factory, dataset, context, user_factory, test_password)
        response = client.post(
            f"/api/v1/reports/{case.ledger_book_code}/render",
            json={"format": "xlsx", "params": RANGE},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert case.ledger_book_code in response.headers["content-disposition"]
        totals = _grand_total(response.content)
        # Mỗi dòng phát sinh mang đúng MỘT bên (Nợ hoặc Có), nên tổng Nợ =
        # tổng Có = 1.000.000 + 200.000 — cân nhau là BR-GLE-04 thu nhỏ.
        expected = Decimal(REVENUE + EXPENSE)
        assert totals == (expected, expected)

    def test_financial_statements_come_from_scheme_layouts_with_same_row_codes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        scheme_world: tuple[SchemeCase, DatasetRef, PostingContext],
        user_factory: UserFactory,
        test_password: str,
    ) -> None:
        """BCTC lập từ layout của đúng gói builtin; chỉ tiêu "110" (tiền) và
        "01" (doanh thu) mang cùng mã dòng ở cả hai thông tư — nên phép khẳng
        định số liệu là một, chỉ MẪU đổi theo scheme."""
        case, dataset, context = scheme_world
        headers = _headers(client, session_factory, dataset, context, user_factory, test_password)
        period = _period_id(session_factory, dataset, context, 2)

        listing = client.get("/api/v1/statements", params={"period_id": period}, headers=headers)
        assert listing.status_code == 200, listing.text
        layout_codes = {item["code"] for item in listing.json()["layouts"]}
        assert {case.balance_layout, case.income_layout} <= layout_codes

        balance = client.get(
            f"/api/v1/statements/{case.balance_layout}/preview",
            params={"period_id": period},
            headers=headers,
        )
        assert balance.status_code == 200, balance.text
        cash_row = next(row for row in balance.json()["rows"] if row["row_code"] == "110")
        assert Decimal(cash_row["value"]) == Decimal(REVENUE - EXPENSE)

        income = client.get(
            f"/api/v1/statements/{case.income_layout}/preview",
            params={"period_id": period},
            headers=headers,
        )
        assert income.status_code == 200, income.text
        revenue_row = next(row for row in income.json()["rows"] if row["row_code"] == "01")
        assert Decimal(revenue_row["value"]) == Decimal(REVENUE)
