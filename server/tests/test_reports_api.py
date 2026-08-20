"""Report engine trọn vòng trên PostgreSQL thật (lát 5C): `/api/v1/reports`.

Bất biến bám tiêu chí phase-05:

* **BR-RPT-01**: chứng từ nháp không xuất hiện trên bất kỳ báo cáo nào —
  `gl_postings` chỉ có chứng từ đã ghi sổ.
* **BR-RPT-04**: đổi `ledger` cho hai bộ số khác nhau trên CÙNG báo cáo
  (một chứng từ chỉ-sổ-tài-chính làm hai sổ lệch đúng số tiền đó).
* **BR-RPT-05 + RT-04**: cộng đa chi nhánh theo dòng phát sinh; người dùng bị
  giới hạn chi nhánh không thấy số của chi nhánh khác (RLS, không phải filter
  tầng ứng dụng); `branch_ids` chỉ thu hẹp thêm.
* **Tiêu chí "thêm báo cáo bằng dữ liệu"**: chèn 4 dòng metadata lúc server
  đang chạy → báo cáo mới render được ngay, không sửa code, không restart.
* Quyền: `view` cho danh mục/spec, `export` cho kết xuất; mã lạ 404; tham số
  sai 422 (FR-RPT-002).

Dataset riêng (`rpt5c`) — sổ sách ở đây là bộ số khép kín, không dùng chung
`dataset_alpha` (cùng lý do `test_statement_builder_api.py`). Việc provision
dataset này đồng thời là test tích hợp của `ensure_builtin_reports` (seed +
probe `LIMIT 0` chạy trong `provision_dataset`).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.reports.models import (
    ReportDataset,
    ReportDefinition,
    ReportLayout,
    ReportParamSet,
)
from ket.kernel.config.reports.seed import ensure_builtin_reports
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.persistence.unit_of_work import unit_of_work
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
from ket.reporting.engine import REPORT_EXPORT, REPORT_VIEW
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"
_DATASET_CODE = "rpt5c"

JAN_10 = date(2026, 1, 10)
FEB_05 = date(2026, 2, 5)
RANGE = {"from_date": "2026-01-01", "to_date": "2026-12-31"}


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def report_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(owner_engine, code=_DATASET_CODE, name="Báo cáo 5C", scheme="TT99")
    yield ref
    drop_dataset_schema(owner_engine, _DATASET_CODE)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], report_dataset: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, report_dataset)


@pytest.fixture(scope="module")
def context_b(session_factory: sessionmaker[Session], report_dataset: DatasetRef) -> PostingContext:
    """Chi nhánh thứ hai trong CÙNG dataset — cho BR-RPT-05 và RLS."""
    return seed_posting_context(session_factory, report_dataset)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    report_dataset: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(report_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _post_journal(
    session: Session,
    context: PostingContext,
    *,
    posting_date: date,
    lines: tuple[tuple[str, int, int], ...],
    post: bool = True,
) -> None:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=VND,
            exchange_rate=Decimal(1),
            description="chứng từ test report engine",
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
    session: Session, context: PostingContext, *, posting_date: date, amount: int
) -> None:
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
            financial_lines=(line("642", debit=amount), line("111", credit=amount)),
            management_lines=(),
        ),
        user_id=ACTOR_ID,
    )


_seeded: set[int] = set()


def _seed_books(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    context_b: PostingContext,
) -> None:
    """Bộ sổ chuẩn của tệp — gieo một lần cho mỗi lần provision dataset.

    Chi nhánh A: thu 1.000.000 (111/511), chi 200.000 (642/111) cả hai sổ;
    chi 50.000 CHỈ sổ tài chính; một chứng từ NHÁP 999.999 (không được lên số).
    Chi nhánh B: thu 400.000 (111/511).
    → Sổ tài chính toàn DN: tổng Nợ = tổng Có = 1.650.000; sổ quản trị 1.600.000;
    riêng chi nhánh A (tài chính) 1.250.000.
    """
    if context.branch_id in _seeded:
        return
    _seeded.add(context.branch_id)
    with unit_of_work(
        session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
    ) as session:
        _post_journal(
            session,
            context,
            posting_date=JAN_10,
            lines=(("111", 1_000_000, 0), ("511", 0, 1_000_000)),
        )
        _post_journal(
            session,
            context,
            posting_date=FEB_05,
            lines=(("642", 200_000, 0), ("111", 0, 200_000)),
        )
        _post_financial_only(session, context, posting_date=FEB_05, amount=50_000)
        _post_journal(
            session,
            context,
            posting_date=FEB_05,
            lines=(("111", 999_999, 0), ("511", 0, 999_999)),
            post=False,
        )
    with unit_of_work(
        session_factory, posting_scope(dataset, context_b, user_id=ACTOR_ID)
    ) as session:
        _post_journal(
            session,
            context_b,
            posting_date=JAN_10,
            lines=(("111", 400_000, 0), ("511", 0, 400_000)),
        )


@pytest.fixture(scope="module")
def exporter_role(session_factory: sessionmaker[Session], report_dataset: DatasetRef) -> str:
    return ensure_role(
        session_factory, report_dataset, "xuat_bao_cao", [REPORT_VIEW, REPORT_EXPORT]
    )


@pytest.fixture(scope="module")
def viewer_only_role(session_factory: sessionmaker[Session], report_dataset: DatasetRef) -> str:
    return ensure_role(session_factory, report_dataset, "chi_xem_bao_cao", [REPORT_VIEW])


def _grand_total_row(sheet: openpyxl.worksheet.worksheet.Worksheet) -> list[object]:
    for row in sheet.iter_rows(values_only=True):
        if row and row[0] == "Tổng cộng":
            return list(row)
    raise AssertionError("không thấy dòng Tổng cộng trong tệp xuất")


class TestReportsApi:
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
        *,
        branch_codes: list[str] | None = None,
    ) -> dict[str, str]:
        headers = actor(
            client,
            session_factory,
            dataset,
            user_factory,
            role,
            prefix,
            password,
            branch_codes=branch_codes or all_branch_codes(session_factory, dataset),
        )
        return {**headers, BRANCH_HEADER: str(branch_id)}

    def _render(
        self,
        client: TestClient,
        headers: dict[str, str],
        *,
        output_format: str = "xlsx",
        params: dict[str, object] | None = None,
        code: str = "SO-CAI",
    ) -> bytes:
        response = client.post(
            f"/api/v1/reports/{code}/render",
            json={"format": output_format, "params": {**RANGE, **(params or {})}},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.content

    def _grand_total(self, content: bytes) -> tuple[Decimal, Decimal]:
        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        sheet = workbook.active
        assert sheet is not None
        row = _grand_total_row(sheet)
        numbers = [value for value in row if isinstance(value, (int, float, Decimal))]
        assert len(numbers) == 2, row
        return Decimal(str(numbers[0])), Decimal(str(numbers[1]))

    def test_catalog_and_params(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        viewer_only_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            viewer_only_role,
            "rpt-xem",
            test_password,
            context.branch_id,
        )
        listing = client.get("/api/v1/reports", headers=headers)
        assert listing.status_code == 200
        codes = {item["code"] for item in listing.json()["reports"]}
        assert "SO-CAI" in codes

        params = client.get("/api/v1/reports/SO-CAI/params", headers=headers)
        assert params.status_code == 200
        body = params.json()
        assert body["ledger_scope"] == "both"
        assert [p["name"] for p in body["params"]] == ["account_code"]

        assert client.get("/api/v1/reports/KHONG-CO/params", headers=headers).status_code == 404

    def test_render_needs_export_permission(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        viewer_only_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            viewer_only_role,
            "rpt-xem2",
            test_password,
            context.branch_id,
        )
        response = client.post(
            "/api/v1/reports/SO-CAI/render",
            json={"format": "xlsx", "params": RANGE},
            headers=headers,
        )
        assert response.status_code == 403

    def test_invalid_params_rejected(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
    ) -> None:
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-loi",
            test_password,
            context.branch_id,
        )
        for params in (
            {"from_date": "2026-12-31", "to_date": "2026-01-01"},
            {**RANGE, "tai_khoan": "111"},
            {**RANGE, "branch_ids": []},
        ):
            response = client.post(
                "/api/v1/reports/SO-CAI/render",
                json={"format": "xlsx", "params": params},
                headers=headers,
            )
            assert response.status_code == 422, params

    def test_ledger_param_gives_two_books_and_drafts_are_invisible(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
        context_b: PostingContext,
    ) -> None:
        """BR-RPT-04 + BR-RPT-01 + BR-RPT-05 trên tệp XLSX thật."""
        _seed_books(session_factory, report_dataset, context, context_b)
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-xuat",
            test_password,
            context.branch_id,
        )
        financial = self._grand_total(self._render(client, headers))
        assert financial == (Decimal(1_650_000), Decimal(1_650_000))

        management = self._grand_total(
            self._render(client, headers, params={"ledger": int(Ledger.MANAGEMENT)})
        )
        assert management == (Decimal(1_600_000), Decimal(1_600_000))

        # Thu hẹp bằng branch_ids trong phạm vi (BR-RPT-05).
        only_a = self._grand_total(
            self._render(client, headers, params={"branch_ids": [context.branch_id]})
        )
        assert only_a == (Decimal(1_250_000), Decimal(1_250_000))

        # Lọc theo tham số ngoài bộ chuẩn: chỉ TK 511.
        only_511 = self._grand_total(self._render(client, headers, params={"account_code": "511"}))
        assert only_511 == (Decimal(0), Decimal(1_400_000))

    def test_rls_hides_other_branch_from_restricted_user(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
        context_b: PostingContext,
    ) -> None:
        """RT-04: cô lập bằng RLS trên bảng gốc — người chỉ được gán chi nhánh
        A render "toàn bộ trong phạm vi" vẫn không thấy số chi nhánh B."""
        _seed_books(session_factory, report_dataset, context, context_b)
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-cn-a",
            test_password,
            context.branch_id,
            branch_codes=[context.branch_code],
        )
        totals = self._grand_total(self._render(client, headers))
        assert totals == (Decimal(1_250_000), Decimal(1_250_000))

    def test_pdf_renders_with_totals(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
        context_b: PostingContext,
    ) -> None:
        _seed_books(session_factory, report_dataset, context, context_b)
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-pdf",
            test_password,
            context.branch_id,
        )
        pdf = self._render(client, headers, output_format="pdf")
        assert pdf.startswith(b"%PDF")
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        assert "1.650.000" in text
        assert "Tổng cộng" in text

    def test_new_report_registered_by_data_only_no_restart(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
        context_b: PostingContext,
        run: Runner,
    ) -> None:
        """Tiêu chí phase-05: thêm 1 báo cáo CHỈ bằng chèn dữ liệu, server đang
        chạy — không sửa engine, không restart."""
        _seed_books(session_factory, report_dataset, context, context_b)

        def register(session: Session) -> None:
            if session.get(ReportDataset, "thu_chi_5c") is not None:
                return
            session.add(
                ReportDataset(
                    code="thu_chi_5c",
                    sql_text=(
                        "SELECT p.branch_id, p.ledger, p.posting_date, "
                        "p.description, p.debit, p.credit\n"
                        "FROM gl_postings p\n"
                        "WHERE p.posting_date >= :from_date AND p.posting_date <= :to_date"
                    ),
                    allowed_params=["from_date", "to_date", "ledger", "branch_ids"],
                    # Tường minh, không dựa mặc định: mặc định là KHÔNG-tin-cậy
                    # (RT-07, review 5C M1) — người quản trị đăng ký báo cáo
                    # phải tự nói "SQL này chạy bằng quyền runtime được".
                    is_builtin=True,
                    description="Bảng kê phát sinh phẳng — test đăng ký lúc chạy",
                )
            )
            session.add(
                ReportLayout(
                    code="thu-chi-5c",
                    kind="table",
                    spec={
                        "columns": [
                            {"key": "posting_date", "label": "Ngày", "type": "date"},
                            {"key": "description", "label": "Diễn giải", "type": "text"},
                            {"key": "debit", "label": "Nợ", "type": "money"},
                            {"key": "credit", "label": "Có", "type": "money"},
                        ],
                        "totals": ["debit", "credit"],
                        "sort": ["posting_date"],
                    },
                )
            )
            session.add(ReportParamSet(code="thu_chi_5c_params", spec={"params": []}))
            session.flush()
            session.add(
                ReportDefinition(
                    code="THU-CHI-5C",
                    name="Bảng kê phát sinh (test)",
                    category="test",
                    module="general_ledger",
                    dataset_code="thu_chi_5c",
                    layout_code="thu-chi-5c",
                    param_set_code="thu_chi_5c_params",
                )
            )

        run(register)
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-moi",
            test_password,
            context.branch_id,
        )
        listing = client.get("/api/v1/reports", headers=headers)
        assert "THU-CHI-5C" in {item["code"] for item in listing.json()["reports"]}
        totals = self._grand_total(self._render(client, headers, code="THU-CHI-5C"))
        assert totals == (Decimal(1_650_000), Decimal(1_650_000))

    def test_non_builtin_dataset_refuses_to_execute(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        user_factory: UserFactory,
        exporter_role: str,
        test_password: str,
        context: PostingContext,
        run: Runner,
    ) -> None:
        """Cổng fail-closed RT-07 (review 5C, H1): dataset KHÔNG-builtin — hạng
        của SQL từ gói `.zip` nhập ngoài — phải bị từ chối chạy cho tới khi 5D
        mang role read-only RLS-bound. Dataset dưới đây cố ý KHÔNG khai
        `is_builtin`: mặc định không-tin-cậy (M1) chính là thứ giữ cổng khi
        đường nhập 5D quên đặt cờ."""

        def register(session: Session) -> None:
            if session.get(ReportDataset, "ngoai_5c") is not None:
                return
            session.add(
                ReportDataset(
                    code="ngoai_5c",
                    sql_text=(
                        "SELECT p.branch_id, p.ledger, p.posting_date, "
                        "p.description, p.debit, p.credit FROM gl_postings p"
                    ),
                    allowed_params=["ledger", "branch_ids"],
                )
            )
            session.add(
                ReportLayout(
                    code="ngoai-5c",
                    kind="table",
                    spec={
                        "columns": [
                            {"key": "description", "label": "Diễn giải", "type": "text"},
                            {"key": "debit", "label": "Nợ", "type": "money"},
                        ],
                        "totals": ["debit"],
                        "sort": ["posting_date"],
                    },
                )
            )
            session.add(ReportParamSet(code="ngoai_5c_params", spec={"params": []}))
            session.flush()
            session.add(
                ReportDefinition(
                    code="NGOAI-5C",
                    name="Báo cáo từ gói ngoài (test RT-07)",
                    category="test",
                    module="general_ledger",
                    dataset_code="ngoai_5c",
                    layout_code="ngoai-5c",
                    param_set_code="ngoai_5c_params",
                )
            )

        run(register)
        headers = self._headers(
            client,
            session_factory,
            report_dataset,
            user_factory,
            exporter_role,
            "rpt-ngoai",
            test_password,
            context.branch_id,
        )
        response = client.post(
            "/api/v1/reports/NGOAI-5C/render",
            json={"format": "xlsx", "params": RANGE},
            headers=headers,
        )
        # 409, không phải 422: tham số người dùng không sai — trạng thái hệ
        # thống chưa cho phép chạy dataset này (L2).
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "report.dataset_not_executable"


class TestBuiltinSeed:
    def test_seed_is_idempotent_per_row(
        self, owner_engine: Engine, report_dataset: DatasetRef
    ) -> None:
        with owner_engine.begin() as connection:
            added = ensure_builtin_reports(connection, report_dataset.schema_name)
        assert added == 0

    def test_builtin_rows_present_after_provision(
        self,
        session_factory: sessionmaker[Session],
        report_dataset: DatasetRef,
        context: PostingContext,
    ) -> None:
        scope = posting_scope(report_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            codes = set(session.scalars(select(ReportDefinition.code)))
            assert "SO-CAI" in codes
            dataset = session.get(ReportDataset, "gl_ledger")
            assert dataset is not None and dataset.is_builtin
