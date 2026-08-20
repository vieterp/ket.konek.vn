"""Statement builder + endpoint `/api/v1/statements` trên PostgreSQL thật (lát 5B).

Bất biến bám tiêu chí phase-05 (điều chỉnh mã số theo TT99 — mẫu mới dùng
280/440 thay 270 của TT200):

* **Cân**: sau kết chuyển, tổng tài sản = tổng nguồn vốn ([440] == [100]+[200]
  thu gọn thành [440] == [100] trong layout test một nhóm) — BR-GLE-04.
* **Hai cột đúng nghĩa**: balance_sheet so "Số đầu năm" (bằng 0 khi năm không
  có số dư ban đầu); income là lũy kế từ đầu năm tới cuối kỳ đã chọn.
* **BR-RPT-04**: đổi `ledger` cho hai bộ số khác nhau trên cùng mẫu.
* **BR-RPT-01**: chứng từ nháp (chưa ghi sổ) không làm thay đổi con số.
* Quyền `reporting.statement.view` là cổng; layout lạ trả 404.

Layout test gắn vào gói TT99-TEST của `posting_support` (gói thắng
`resolve_package` trong dataset test) — builder phải đọc đúng bộ layout của
gói đang hiệu lực, đó chính là điều test này chứng minh.

Tệp này dùng **dataset riêng** (không dùng `dataset_alpha`): sổ sách của nó là
một bộ số khép kín (cân sau kết chuyển, lũy kế theo kỳ), trong khi năm tài
chính `2026` của `posting_support` là tài sản DÙNG CHUNG toàn `dataset_alpha` —
tệp khác khóa kỳ / quyết toán năm / kích hoạt gói là con số ở đây đổi theo thứ
tự chạy. Cùng lý do `test_config_package_activator.py` tự cấp dataset.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, ConfigPackage
from ket.kernel.config.statements.models import StatementLayout, StatementRow
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.errors import StatementLayoutNotFoundError
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.main import create_app
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import (
    JOURNAL_NUMBERING_RULE,
    JournalVoucherService,
)
from ket.posting.documents.service import VoucherDraft, VoucherService
from ket.posting.engine.models import Ledger
from ket.posting.engine.requests import PostingLine, PostingRequest
from ket.posting.engine.service import PostingService
from ket.posting.opening_balances.models import OpeningBalance
from ket.reporting.statements import STATEMENT_VIEW
from ket.reporting.statements.builder import build_statement, list_layouts
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"

JAN_10 = date(2026, 1, 10)
FEB_05 = date(2026, 2, 5)
FEB_20 = date(2026, 2, 20)
MAR_10 = date(2026, 3, 10)
MAR_31 = date(2026, 3, 31)

_EXTRA_ACCOUNTS: tuple[tuple[str, str, int], ...] = (
    ("411", "Vốn đầu tư của chủ sở hữu", BalanceNature.CREDIT),
    ("421", "Lợi nhuận sau thuế chưa phân phối", BalanceNature.DUAL),
    ("911", "Xác định kết quả kinh doanh", BalanceNature.NONE),
)

_TEST_B01_ROWS: tuple[tuple[str, str], ...] = (
    ("110", "DR(11*)"),
    ("130", "DR(131)"),
    ("100", "[110] + [130]"),
    ("300", "CR(331)"),
    ("400", "CR(411) - BAL(421)"),
    ("440", "[300] + [400]"),
)
_TEST_B02_ROWS: tuple[tuple[str, str], ...] = (
    ("01", "CR_PS(511)"),
    ("02", "DR_PS(511)"),
    ("10", "[01] - [02]"),
    ("26", "DR_NET(642)"),
    ("60", "[10] - [26]"),
)


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


_DATASET_CODE = "bctc5b"


@pytest.fixture(scope="module")
def statement_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(owner_engine, code=_DATASET_CODE, name="BCTC lát 5B", scheme="TT99")
    yield ref
    drop_dataset_schema(owner_engine, _DATASET_CODE)


@pytest.fixture(scope="module")
def context(
    session_factory: sessionmaker[Session], statement_dataset: DatasetRef
) -> PostingContext:
    return seed_posting_context(session_factory, statement_dataset)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    statement_dataset: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(statement_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _ensure_layouts(session: Session, context: PostingContext) -> None:
    """TK bổ sung + hai layout test trên gói TT99-TEST — idempotent theo mã.

    Đồng thời làm mới `activated_at` của gói: `resolve_package` xếp theo
    `(effective_from, activated_at, id)` giảm dần, nên gói test phải là gói
    kích hoạt gần nhất để test không phụ thuộc thứ tự chạy của các tệp test
    khác (cùng lý do đã ghi ở `posting_support._ensure_package_and_accounts`).
    """
    package = session.get(ConfigPackage, context.package_id)
    assert package is not None
    package.activated_at = datetime.now(UTC)

    for code, name, nature in _EXTRA_ACCOUNTS:
        existing = session.scalar(
            select(ChartOfAccount.id).where(
                ChartOfAccount.package_id == context.package_id, ChartOfAccount.code == code
            )
        )
        if existing is not None:
            context.accounts[code] = existing
            continue
        account = ChartOfAccount(
            package_id=context.package_id,
            code=code,
            name=name,
            path="0.",
            balance_nature=nature,
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        context.accounts[code] = account.id

    for layout_code, kind, rows in (
        ("TEST-B01", "balance_sheet", _TEST_B01_ROWS),
        ("TEST-B02", "income", _TEST_B02_ROWS),
    ):
        exists = session.scalar(
            select(StatementLayout.id).where(
                StatementLayout.package_id == context.package_id,
                StatementLayout.code == layout_code,
            )
        )
        if exists is not None:
            continue
        layout = StatementLayout(
            package_id=context.package_id,
            code=layout_code,
            name=f"Layout test {layout_code}",
            statement_kind=kind,
        )
        session.add(layout)
        session.flush()
        for order, (row_code, formula) in enumerate(rows, start=1):
            session.add(
                StatementRow(
                    layout_id=layout.id,
                    row_code=row_code,
                    label=f"Chỉ tiêu {row_code}",
                    formula=formula,
                    display_order=order,
                )
            )
    session.flush()


def _post_journal(
    session: Session,
    context: PostingContext,
    *,
    posting_date: date,
    lines: tuple[tuple[str, int, int], ...],
    post: bool = True,
) -> None:
    """Một chứng từ GLE nhiều dòng: `(mã TK, nợ, có)` — ghi sổ cả hai sổ."""
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=VND,
            exchange_rate=Decimal(1),
            description="chứng từ test statement",
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
    if post:
        service.post(voucher.id, user_id=ACTOR_ID)


def _post_financial_only(
    session: Session,
    context: PostingContext,
    *,
    posting_date: date,
    debit_account: str,
    credit_account: str,
    amount: int,
) -> None:
    """Một chứng từ chỉ lên sổ tài chính (`management_lines=()`) — BR-RPT-04."""
    voucher = VoucherService(session).create(
        VoucherDraft(
            document_type="GLE",
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=VND,
            exchange_rate=Decimal(1),
        ),
        rule=JOURNAL_NUMBERING_RULE,
        user_id=ACTOR_ID,
    )

    def line(account: str, *, debit: int = 0, credit: int = 0) -> PostingLine:
        return PostingLine(
            account_id=context.accounts[account],
            debit_fc=Decimal(debit),
            credit_fc=Decimal(credit),
            currency=VND,
            rate=Decimal(1),
        )

    PostingService(session).post(
        PostingRequest(
            voucher_id=voucher.id,
            financial_lines=(
                line(debit_account, debit=amount),
                line(credit_account, credit=amount),
            ),
            management_lines=(),
        ),
        user_id=ACTOR_ID,
    )


_seeded_branches: set[int] = set()


def _seed_books(session: Session, context: PostingContext) -> None:
    """Bộ sổ chuẩn của tệp test này — gieo một lần cho mỗi chi nhánh test.

    Góp vốn, bán chịu, chi phí (cả hai sổ); một khoản chi phí CHỈ sổ tài chính;
    kết chuyển thủ công cuối tháng 3 cho sổ tài chính cân (BR-GLE-04); và một
    chứng từ NHÁP để chứng minh BR-RPT-01.
    """
    if context.branch_id in _seeded_branches:
        return
    _ensure_layouts(session, context)
    _post_journal(
        session, context, posting_date=JAN_10, lines=(("111", 1_000_000, 0), ("411", 0, 1_000_000))
    )
    # Bán thu ngay qua ngân hàng — không dùng 131 vì TK đó bắt buộc chiều
    # khách hàng (FR-SYS-021), ngoài phạm vi test statement.
    _post_journal(
        session, context, posting_date=FEB_05, lines=(("112", 500_000, 0), ("511", 0, 500_000))
    )
    _post_journal(
        session, context, posting_date=FEB_20, lines=(("642", 200_000, 0), ("111", 0, 200_000))
    )
    _post_financial_only(
        session,
        context,
        posting_date=MAR_10,
        debit_account="642",
        credit_account="111",
        amount=50_000,
    )
    # Kết chuyển thủ công (chưa có engine kết chuyển — 10a): sổ tài chính khép
    # 511/642 về 911 rồi 421. Sổ quản trị nhận cùng bút toán (journal nhân bản)
    # nên lệch 50k ở 642 — đúng bản chất "hai sổ độc lập", không kiểm cân ở đây.
    _post_journal(
        session,
        context,
        posting_date=MAR_31,
        lines=(
            ("511", 500_000, 0),
            ("911", 0, 500_000),
            ("911", 250_000, 0),
            ("642", 0, 250_000),
            ("911", 250_000, 0),
            ("421", 0, 250_000),
        ),
    )
    # Chứng từ nháp — không được lọt vào bất kỳ con số nào (BR-RPT-01).
    _post_journal(
        session,
        context,
        posting_date=FEB_20,
        lines=(("642", 999_999, 0), ("111", 0, 999_999)),
        post=False,
    )
    _seeded_branches.add(context.branch_id)


@pytest.fixture
def books(run: Runner, context: PostingContext) -> PostingContext:
    """Gieo sổ trong transaction RIÊNG, tách khỏi transaction chạy assertion —
    assertion đỏ không cuộn ngược sổ đã gieo trong khi cờ `_seeded_branches`
    còn set (review 5B, L-1)."""
    run(lambda session: _seed_books(session, context))
    return context


def _period_id(session: Session, context: PostingContext, month: int) -> int:
    period_id = session.scalar(
        select(AccountingPeriod.id).where(
            AccountingPeriod.fiscal_year_id == context.fiscal_year_id,
            AccountingPeriod.period_no == month,
        )
    )
    assert period_id is not None
    return period_id


def _values(rows: tuple[object, ...]) -> dict[str, Decimal]:
    return {row.row_code: row.value for row in rows}  # type: ignore[attr-defined]


class TestStatementBuilder:
    def test_balance_sheet_balances_after_closing_and_opening_column_is_zero(
        self, run: Runner, books: PostingContext
    ) -> None:
        context = books

        def work(session: Session) -> object:
            result = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=_period_id(session, context, 3),
                ledger=Ledger.FINANCIAL,
                branch_id=context.branch_id,
            )
            values = _values(result.rows)
            assert values["110"] == Decimal("1250000.00")
            assert values["130"] == Decimal("0")
            assert values["100"] == Decimal("1250000.00")
            assert values["300"] == Decimal("0")
            assert values["400"] == Decimal("1250000.00")
            # Tổng tài sản = tổng nguồn vốn (BR-GLE-04) — mẫu TT99 là
            # [280] == [440]; layout test thu gọn thành [100] == [440].
            assert values["440"] == values["100"]
            # "Số đầu năm": chi nhánh này không có số dư ban đầu → mọi chỉ tiêu 0
            # (ca CÓ số dư ban đầu kiểm ở TestStatementSource).
            assert all(row.comparative == Decimal(0) for row in result.rows)
            return None

        run(work)

    def test_income_statement_is_year_to_date_at_the_chosen_period(
        self, run: Runner, books: PostingContext
    ) -> None:
        context = books

        def work(session: Session) -> object:
            result = build_statement(
                session,
                layout_code="TEST-B02",
                period_id=_period_id(session, context, 2),
                ledger=Ledger.FINANCIAL,
                branch_id=context.branch_id,
            )
            values = _values(result.rows)
            assert values["01"] == Decimal("500000.00")
            assert values["02"] == Decimal("0")
            assert values["10"] == Decimal("500000.00")
            assert values["26"] == Decimal("200000.00")
            assert values["60"] == Decimal("300000.00")
            # Cột "Năm trước" của layout phát sinh CHƯA lập được ở v1 (H1
            # review 5B): builder trả None, không phải 0.
            assert all(row.comparative is None for row in result.rows)
            return None

        run(work)

    def test_income_statement_after_closing_reads_raw_turnover_including_closing_entries(
        self, run: Runner, books: PostingContext
    ) -> None:
        """GHIM hành vi đã biết (H1 review 5B): xem B02 tại kỳ SAU bút toán kết
        chuyển thủ công thì phát sinh thô nhiễm chính bút toán kết chuyển —
        Mã 02 (DR_PS 511) nuốt trọn kết chuyển 511→911, Mã 10/60 về 0. Đây là
        giới hạn ngữ nghĩa đã ghi ở `note` chỉ tiêu 01/02 và docstring builder;
        10a mang cờ loại-trừ-kết-chuyển thì test này PHẢI đổi kỳ vọng."""
        context = books

        def work(session: Session) -> object:
            result = build_statement(
                session,
                layout_code="TEST-B02",
                period_id=_period_id(session, context, 3),
                ledger=Ledger.FINANCIAL,
                branch_id=context.branch_id,
            )
            values = _values(result.rows)
            assert values["01"] == Decimal("500000.00")
            assert values["02"] == Decimal(
                "500000.00"
            )  # = bút toán kết chuyển, KHÔNG phải giảm trừ
            assert values["10"] == Decimal("0")
            assert values["60"] == Decimal("0")
            return None

        run(work)

    def test_ledgers_produce_two_independent_figures_on_the_same_layout(
        self, run: Runner, books: PostingContext
    ) -> None:
        """BR-RPT-04: khoản chi 50k chỉ lên sổ tài chính → 110 hai sổ lệch 50k."""
        context = books

        def work(session: Session) -> object:
            period_id = _period_id(session, context, 3)
            financial = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=period_id,
                ledger=Ledger.FINANCIAL,
                branch_id=context.branch_id,
            )
            management = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=period_id,
                ledger=Ledger.MANAGEMENT,
                branch_id=context.branch_id,
            )
            assert _values(financial.rows)["110"] == Decimal("1250000.00")
            assert _values(management.rows)["110"] == Decimal("1300000.00")
            return None

        run(work)

    def test_unknown_layout_code_raises_not_found(self, run: Runner, books: PostingContext) -> None:
        context = books

        def work(session: Session) -> object:
            with pytest.raises(StatementLayoutNotFoundError):
                build_statement(
                    session,
                    layout_code="KHONG-TON-TAI",
                    period_id=_period_id(session, context, 3),
                    ledger=Ledger.FINANCIAL,
                )
            return None

        run(work)

    def test_list_layouts_returns_the_active_package_and_its_layouts(
        self, run: Runner, books: PostingContext
    ) -> None:
        context = books

        def work(session: Session) -> object:
            package_code, layouts = list_layouts(session, period_id=_period_id(session, context, 1))
            # Gói thắng resolve trong dataset test là TT99-TEST (posting_support).
            assert package_code == "TT99-TEST"
            codes = {layout.code for layout in layouts}
            assert {"TEST-B01", "TEST-B02"} <= codes
            return None

        run(work)


class TestStatementSource:
    """Ba lỗ nguồn số mà review 5B chỉ ra (M4/M5/M6 sống sót): số dư ban đầu
    phải chảy vào CẢ HAI cột, và lọc chi nhánh phải cắt đúng phần phát sinh."""

    @pytest.fixture
    def context_b(
        self, session_factory: sessionmaker[Session], statement_dataset: DatasetRef
    ) -> PostingContext:
        """Chi nhánh thứ hai trong CÙNG dataset — có số dư ban đầu, một chứng từ."""
        return seed_posting_context(session_factory, statement_dataset)

    @pytest.fixture
    def books_b(
        self,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        context_b: PostingContext,
    ) -> PostingContext:
        scope = posting_scope(statement_dataset, context_b, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            if context_b.branch_id not in _seeded_branches:
                _ensure_layouts(session, context_b)
                session.add(
                    OpeningBalance(
                        fiscal_year_id=context_b.fiscal_year_id,
                        ledger=Ledger.FINANCIAL.value,
                        branch_id=context_b.branch_id,
                        account_id=context_b.accounts["111"],
                        currency_code=VND,
                        debit=Decimal("300000.00"),
                        debit_fc=Decimal("300000.00"),
                        detail_kind=0,
                    )
                )
                session.add(
                    OpeningBalance(
                        fiscal_year_id=context_b.fiscal_year_id,
                        ledger=Ledger.FINANCIAL.value,
                        branch_id=context_b.branch_id,
                        account_id=context_b.accounts["411"],
                        currency_code=VND,
                        credit=Decimal("300000.00"),
                        credit_fc=Decimal("300000.00"),
                        detail_kind=0,
                    )
                )
                _post_journal(
                    session,
                    context_b,
                    posting_date=FEB_05,
                    lines=(("112", 100_000, 0), ("411", 0, 100_000)),
                )
                _seeded_branches.add(context_b.branch_id)
        return context_b

    def test_opening_balances_flow_into_both_columns(
        self,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        books_b: PostingContext,
    ) -> None:
        """Giết M4: bỏ `opening` khỏi phép tính dư cuối thì 110 = 100k ≠ 400k;
        cột "Số đầu năm" phải bằng đúng số dư ban đầu, không phải 0."""
        context = books_b
        scope = posting_scope(statement_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            result = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=_period_id(session, context, 2),
                ledger=Ledger.FINANCIAL,
                branch_id=context.branch_id,
            )
            values = _values(result.rows)
            comparative = {row.row_code: row.comparative for row in result.rows}
            assert values["110"] == Decimal("400000.00")
            assert values["400"] == Decimal("400000.00")
            assert values["440"] == values["100"]
            assert comparative["110"] == Decimal("300000.00")
            assert comparative["400"] == Decimal("300000.00")
            assert comparative["440"] == comparative["100"]

    def test_branch_filter_scopes_turnover_to_the_requested_branch(
        self,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        books: PostingContext,
        books_b: PostingContext,
    ) -> None:
        """Giết M6: gỡ lọc `:branch_id` khỏi CTE phát sinh thì chi nhánh B
        nuốt cả phát sinh của A (110 = 1.650.000 thay vì 400.000)."""
        scope = RequestScope(
            dataset_schema=statement_dataset.schema_name,
            user_id=ACTOR_ID,
            branch_ids=(books.branch_id, books_b.branch_id),
            acting_branch_id=books.branch_id,
        )
        with unit_of_work(session_factory, scope) as session:
            period_id = _period_id(session, books, 3)
            only_b = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=period_id,
                ledger=Ledger.FINANCIAL,
                branch_id=books_b.branch_id,
            )
            both = build_statement(
                session,
                layout_code="TEST-B01",
                period_id=period_id,
                ledger=Ledger.FINANCIAL,
                branch_id=None,
            )
            assert _values(only_b.rows)["110"] == Decimal("400000.00")
            # Không truyền branch_id = cộng mọi chi nhánh trong phạm vi
            # (BR-RPT-05): 1.250.000 (A) + 400.000 (B).
            assert _values(both.rows)["110"] == Decimal("1650000.00")


@pytest.fixture(scope="module")
def viewer_role(session_factory: sessionmaker[Session], statement_dataset: DatasetRef) -> str:
    return ensure_role(session_factory, statement_dataset, "xem_bctc", [STATEMENT_VIEW])


@pytest.fixture(scope="module")
def outsider_role(session_factory: sessionmaker[Session], statement_dataset: DatasetRef) -> str:
    return ensure_role(session_factory, statement_dataset, "ngoai_cuoc_bctc", [])


class TestStatementsApi:
    def _headers(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset: DatasetRef,
        user_factory: UserFactory,
        role: str,
        prefix: str,
        password: str,
        branch_id: int,
    ) -> dict[str, str]:
        headers = actor(
            client,
            session_factory,
            dataset,
            user_factory,
            role,
            prefix,
            password,
            branch_codes=all_branch_codes(session_factory, dataset),
        )
        return {**headers, BRANCH_HEADER: str(branch_id)}

    def test_preview_requires_permission(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        user_factory: UserFactory,
        outsider_role: str,
        test_password: str,
        context: PostingContext,
        run: Runner,
    ) -> None:
        run(lambda session: _seed_books(session, context))
        period_id = run(lambda session: _period_id(session, context, 3))
        headers = self._headers(
            client,
            session_factory,
            statement_dataset,
            user_factory,
            outsider_role,
            "bctc-out",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/statements/TEST-B01/preview",
            params={"period_id": period_id},
            headers=headers,
        )
        assert response.status_code == 403

    def test_preview_returns_rows_in_display_order(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        user_factory: UserFactory,
        viewer_role: str,
        test_password: str,
        context: PostingContext,
        run: Runner,
    ) -> None:
        run(lambda session: _seed_books(session, context))
        period_id = run(lambda session: _period_id(session, context, 3))
        headers = self._headers(
            client,
            session_factory,
            statement_dataset,
            user_factory,
            viewer_role,
            "bctc-xem",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/statements/TEST-B01/preview",
            params={"period_id": period_id, "branch_id": context.branch_id},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["layout_code"] == "TEST-B01"
        assert body["statement_kind"] == "balance_sheet"
        assert [row["row_code"] for row in body["rows"]] == [
            "110",
            "130",
            "100",
            "300",
            "400",
            "440",
        ]
        by_code = {row["row_code"]: row for row in body["rows"]}
        assert Decimal(by_code["440"]["value"]) == Decimal("1250000.00")

        listing = client.get("/api/v1/statements", params={"period_id": period_id}, headers=headers)
        assert listing.status_code == 200
        listed = {layout["code"] for layout in listing.json()["layouts"]}
        assert {"TEST-B01", "TEST-B02"} <= listed

    def test_unknown_layout_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        statement_dataset: DatasetRef,
        user_factory: UserFactory,
        viewer_role: str,
        test_password: str,
        context: PostingContext,
        run: Runner,
    ) -> None:
        run(lambda session: _seed_books(session, context))
        period_id = run(lambda session: _period_id(session, context, 3))
        headers = self._headers(
            client,
            session_factory,
            statement_dataset,
            user_factory,
            viewer_role,
            "bctc-404",
            test_password,
            context.branch_id,
        )
        response = client.get(
            "/api/v1/statements/KHONG-CO/preview",
            params={"period_id": period_id},
            headers=headers,
        )
        assert response.status_code == 404
