"""Bộ sổ theo mã mẫu thông tư + preview lưới + in chứng từ (lát 5D) trên
PostgreSQL thật.

Ba nhóm bất biến:

* **Bộ sổ (bước 15)**: `S03a-DN` đánh STT liên tục; `S38-DN` mở đầu mỗi tài
  khoản bằng dòng "Số dư đầu kỳ" và số dư lũy kế khớp phát sinh; `S06-DN`
  cân hai vế ở cả ba bộ cột (BR-GLE-04 áp cho bảng cân đối số phát sinh).
* **Preview (bước 14)**: cùng con số với bản in (BR-RPT-02 tầng trình bày),
  quyền `view` là đủ.
* **In chứng từ (bước 16, FR-RPT-008/011)**: mẫu mặc định gieo sẵn, nháp mang
  dấu BẢN NHÁP, `copy_no` đếm thật + cảnh báo in lại, `print_log` chỉ-thêm,
  công tắc cấm in nháp trả 409.

Dataset RIÊNG (`prt5d`) với bộ sổ khép kín — không đụng `rpt5c` của
`test_reports_api`: fixture context của mô-đun khác là module-scope, import lại
là gieo thêm chi nhánh mới vào dataset chung và mọi phép cộng toàn-DN của cả
hai tệp cùng sai (lỗi lệ-thuộc-thứ-tự đã gặp thật ở lần chạy trọn bộ đầu tiên).
Bộ sổ: A thu 1.000.000 (111/511), chi 200.000 (642/111), một chứng từ NHÁP;
B thu 400.000 → tổng phát sinh tài chính 1.600.000; 111 dư 1.200.000.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.catalog import PRINT_ALLOW_DRAFT_KEY
from ket.kernel.config.settings_service import SettingScope, set_setting
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.general_ledger.journal import (
    JOURNAL_PERMISSION_CODE,
    JOURNAL_PERMISSION_MODULE,
)
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.reporting.engine import REPORT_VIEW
from ket.reporting.printing.models import PrintLog
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"
RANGE = {"from_date": "2026-01-01", "to_date": "2026-02-28"}
"""Cửa sổ của BỘ SỔ chuẩn (tháng 1–2). Chứng từ của nhóm test IN cố ý nằm
tháng 3 — hai nhóm không bao giờ cộng chéo nhau bất kể thứ tự chạy (bài học
lệ-thuộc-thứ-tự đã gặp hai lần trong lát này)."""

JOURNAL_PRINT = permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.PRINT)
JOURNAL_CREATE = permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, Action.CREATE)


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def print_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(owner_engine, code="prt5d", name="Sổ sách + in 5D", scheme="TT99")
    yield ref
    drop_dataset_schema(owner_engine, "prt5d")


@pytest.fixture(scope="module")
def print_context(
    session_factory: sessionmaker[Session], print_dataset: DatasetRef
) -> PostingContext:
    return seed_posting_context(session_factory, print_dataset)


@pytest.fixture(scope="module")
def print_context_b(
    session_factory: sessionmaker[Session], print_dataset: DatasetRef
) -> PostingContext:
    return seed_posting_context(session_factory, print_dataset)


@pytest.fixture(scope="module")
def printer_role(session_factory: sessionmaker[Session], print_dataset: DatasetRef) -> str:
    return ensure_role(session_factory, print_dataset, "in_chung_tu_5d", [JOURNAL_PRINT])


@pytest.fixture(scope="module")
def print_viewer_role(session_factory: sessionmaker[Session], print_dataset: DatasetRef) -> str:
    return ensure_role(session_factory, print_dataset, "in_5d_chi_xem", [REPORT_VIEW])


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
            description="bộ sổ 5D",
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


_books_seeded = False


def _seed_local_books(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    context_b: PostingContext,
) -> None:
    global _books_seeded
    if _books_seeded:
        return
    _books_seeded = True
    with unit_of_work(
        session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
    ) as session:
        _post_journal(
            session,
            context,
            posting_date=date(2026, 1, 10),
            lines=(("111", 1_000_000, 0), ("511", 0, 1_000_000)),
        )
        _post_journal(
            session,
            context,
            posting_date=date(2026, 2, 5),
            lines=(("642", 200_000, 0), ("111", 0, 200_000)),
        )
        _post_journal(
            session,
            context,
            posting_date=date(2026, 2, 5),
            lines=(("111", 999_999, 0), ("511", 0, 999_999)),
            post=False,
        )
    with unit_of_work(
        session_factory, posting_scope(dataset, context_b, user_id=ACTOR_ID)
    ) as session:
        _post_journal(
            session,
            context_b,
            posting_date=date(2026, 1, 10),
            lines=(("111", 400_000, 0), ("511", 0, 400_000)),
        )


def _headers(
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


def _preview(
    client: TestClient,
    headers: dict[str, str],
    code: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/reports/{code}/preview",
        json={"params": params or dict(RANGE)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _data_rows(body: dict[str, object]) -> list[dict[str, object]]:
    rows = body["rows"]
    assert isinstance(rows, list)
    return [row for row in rows if row["kind"] == "data"]


def _cell(body: dict[str, object], row: dict[str, object], key: str) -> str:
    columns = body["columns"]
    assert isinstance(columns, list)
    index = next(i for i, col in enumerate(columns) if col["key"] == key)
    cells = row["cells"]
    assert isinstance(cells, list)
    text = cells[index]["text"]
    assert isinstance(text, str)
    return text


class TestBookPreviews:
    def test_journal_book_numbers_lines_sequentially(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
        print_context_b: PostingContext,
    ) -> None:
        """S03a-DN: STT liên tục 1..N và preview đi bằng quyền `view`."""
        _seed_local_books(session_factory, print_dataset, print_context, print_context_b)
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "sach-nkc",
            test_password,
            print_context.branch_id,
        )
        body = _preview(client, headers, "S03a-DN")
        rows = _data_rows(body)
        # 2 chứng từ ghi sổ × 2 dòng (A) + 1 × 2 dòng (B) = 6 dòng; nháp vô hình.
        assert [_cell(body, row, "stt") for row in rows] == [str(i) for i in range(1, 7)]
        assert body["truncated"] is False
        # H1 (review 5D): chứng từ ĐẦU TIÊN của mỗi chi nhánh cùng mang
        # `GLE26-00001` (đánh số per-branch) — dòng Nợ/Có của MỘT chứng từ phải
        # liền nhau, không đan xen giữa hai chi nhánh trùng số cùng ngày.
        amounts = [(_cell(body, row, "debit"), _cell(body, row, "credit")) for row in rows]
        assert amounts == [
            ("1.000.000", ""),
            ("", "1.000.000"),
            ("400.000", ""),
            ("", "400.000"),
            ("200.000", ""),
            ("", "200.000"),
        ]

    def test_detail_book_opens_each_account_with_opening_balance_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
        print_context_b: PostingContext,
    ) -> None:
        """S38-DN trên TK 111: dòng "Số dư đầu kỳ" đứng đầu, số dư lũy kế
        sau từng dòng khớp phát sinh, dư cuối = 1.200.000 (A 800k + B 400k)."""
        _seed_local_books(session_factory, print_dataset, print_context, print_context_b)
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "sach-ct",
            test_password,
            print_context.branch_id,
        )
        body = _preview(client, headers, "S38-DN", {**RANGE, "account_code": "111"})
        rows = _data_rows(body)
        assert _cell(body, rows[0], "description") == "Số dư đầu kỳ"
        assert _cell(body, rows[0], "balance_debit") == ""  # dư đầu 0 → ô trống
        balances = [_cell(body, row, "balance_debit") for row in rows[1:]]
        assert balances[-1] == "1.200.000"
        # Lũy kế đơn điệu theo đúng thứ tự hiển thị — con số từng bước là
        # bằng chứng cửa sổ tính trùng thứ tự sort (bất biến của gl_detail.sql).
        assert "1.000.000" in balances[0] or balances[0] == "1.000.000"

    def test_trial_balance_balances_on_all_three_column_pairs(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
        print_context_b: PostingContext,
    ) -> None:
        """S06-DN: tổng Nợ = tổng Có ở dư đầu / phát sinh / dư cuối."""
        _seed_local_books(session_factory, print_dataset, print_context, print_context_b)
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "sach-cdps",
            test_password,
            print_context.branch_id,
        )
        body = _preview(client, headers, "S06-DN")
        rows = body["rows"]
        assert isinstance(rows, list)
        grand = next(row for row in rows if row["kind"] == "grand_total")
        cells = grand["cells"]
        assert isinstance(cells, list)
        # Ô đầu là nhãn; sáu ô sau theo thứ tự cột tổng của layout.
        texts = [cell["text"] for cell in cells[1:]]
        (
            opening_debit,
            opening_credit,
            period_debit,
            period_credit,
            closing_debit,
            closing_credit,
        ) = texts
        assert opening_debit == opening_credit == "0"
        assert period_debit == period_credit == "1.600.000"
        assert closing_debit == closing_credit == "1.400.000"
        by_account = {_cell(body, row, "account_code"): row for row in _data_rows(body)}
        assert _cell(body, by_account["111"], "closing_debit") == "1.200.000"
        assert _cell(body, by_account["511"], "closing_credit") == "1.400.000"
        assert _cell(body, by_account["642"], "closing_debit") == "200.000"


class TestVoucherPrinting:
    def _make_voucher(
        self,
        session_factory: sessionmaker[Session],
        dataset: DatasetRef,
        context: PostingContext,
        *,
        amount: int,
        post: bool,
    ) -> UUID:
        with unit_of_work(
            session_factory, posting_scope(dataset, context, user_id=ACTOR_ID)
        ) as session:
            service = JournalVoucherService(session)
            voucher = service.create(
                JournalVoucherIn(
                    branch_id=context.branch_id,
                    document_date=date(2026, 3, 1),
                    posting_date=date(2026, 3, 1),
                    currency_code=VND,
                    exchange_rate=Decimal(1),
                    description="phiếu kế toán để in",
                    lines=(
                        JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(amount)),
                        JournalLineIn(
                            account_id=context.accounts["111"], credit_fc=Decimal(amount)
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
            if post:
                service.post(voucher.id, user_id=ACTOR_ID)
            return voucher.id

    def test_templates_listing_and_default_print_flow(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        printer_role: str,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
    ) -> None:
        """Trọn vòng FR-RPT-008/011: mẫu mặc định → in lần 1 → in lần 2 có
        cảnh báo, `print_log` ghi từng lần, thiếu quyền `print` là 403."""
        voucher_id = self._make_voucher(
            session_factory, print_dataset, print_context, amount=123_000, post=True
        )
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            printer_role,
            "in-ct",
            test_password,
            print_context.branch_id,
        )

        listing = client.get(
            "/api/v1/print-templates", params={"document_type": "GLE"}, headers=headers
        )
        assert listing.status_code == 200
        templates = listing.json()["templates"]
        assert [(t["code"], t["is_default"]) for t in templates] == [("PHIEU-KE-TOAN", True)]

        first = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
        assert first.status_code == 200, first.text
        assert first.headers["content-type"].startswith("application/pdf")
        assert first.headers["X-Print-Copy-No"] == "1"
        assert first.headers["X-Print-Reprint"] == "false"
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(first.content)).pages)
        assert "PHIẾU KẾ TOÁN" in text.upper()
        assert "123.000" in text
        assert "BẢN NHÁP" not in text

        second = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
        assert second.headers["X-Print-Copy-No"] == "2"
        assert second.headers["X-Print-Reprint"] == "true"
        reprint_text = "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(second.content)).pages
        )
        assert "In lần 2" in reprint_text

        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            copies = list(
                session.scalars(
                    select(PrintLog.copy_no)
                    .where(PrintLog.voucher_id == voucher_id)
                    .order_by(PrintLog.copy_no)
                )
            )
            assert copies == [1, 2]

        unknown = client.post(
            f"/api/v1/vouchers/{voucher_id}/print",
            json={"template_code": "KHONG-CO"},
            headers=headers,
        )
        assert unknown.status_code == 404

        no_print_headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "in-cam",
            test_password,
            print_context.branch_id,
        )
        denied = client.post(
            f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=no_print_headers
        )
        assert denied.status_code == 403

    def test_draft_prints_with_watermark_until_the_switch_is_off(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        printer_role: str,
        test_password: str,
        print_context: PostingContext,
    ) -> None:
        """FR-RPT-011: nháp in được kèm BẢN NHÁP; tắt công tắc thì 409."""
        draft_id = self._make_voucher(
            session_factory, print_dataset, print_context, amount=77_000, post=False
        )
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            printer_role,
            "in-nhap",
            test_password,
            print_context.branch_id,
        )
        printed = client.post(f"/api/v1/vouchers/{draft_id}/print", json={}, headers=headers)
        assert printed.status_code == 200, printed.text
        text = "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(printed.content)).pages
        )
        assert "BẢN NHÁP" in text

        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            set_setting(
                session,
                key=PRINT_ALLOW_DRAFT_KEY,
                scope=SettingScope.SYSTEM,
                user_id=ACTOR_ID,
                raw_value="false",
                expected_row_version=None,
            )
        try:
            blocked = client.post(f"/api/v1/vouchers/{draft_id}/print", json={}, headers=headers)
            assert blocked.status_code == 409
            assert blocked.json()["error_code"] == "print.not_allowed"
        finally:
            with unit_of_work(
                session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
            ) as session:
                set_setting(
                    session,
                    key=PRINT_ALLOW_DRAFT_KEY,
                    scope=SettingScope.SYSTEM,
                    user_id=ACTOR_ID,
                    raw_value="true",
                    expected_row_version=1,
                )

    def test_print_log_is_append_only_for_the_runtime_role(
        self,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        print_context: PostingContext,
    ) -> None:
        """`print_log` thuộc `APPEND_ONLY_TABLES`: vai trò runtime không
        UPDATE/DELETE được lịch sử in — bất biến FR-RPT-011 nằm ở tầng DB."""
        from sqlalchemy import text as sql_text
        from sqlalchemy.exc import ProgrammingError

        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            with pytest.raises(ProgrammingError, match="permission denied"):
                session.execute(sql_text("DELETE FROM print_log"))


class TestSchemeScopedCatalog:
    def test_a_tt99_dataset_neither_lists_nor_serves_tt133_form_codes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
        print_context_b: PostingContext,
    ) -> None:
        """H2 (review 5D): `package_id` ghi lúc seed phải có đường đọc —
        doanh nghiệp TT99 không thấy và không render được mã mẫu TT133."""
        _seed_local_books(session_factory, print_dataset, print_context, print_context_b)
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "scheme-loc",
            test_password,
            print_context.branch_id,
        )
        listing = client.get("/api/v1/reports", headers=headers)
        assert listing.status_code == 200
        codes = {item["code"] for item in listing.json()["reports"]}
        assert {"S03a-DN", "S03b-DN", "S38-DN", "S06-DN"} <= codes
        assert not codes.intersection({"S03a-DNN", "S03b-DNN", "S19-DNN", "F01-DNN"})

        for code in ("F01-DNN", "S03a-DNN"):
            response = client.post(
                f"/api/v1/reports/{code}/preview", json={"params": dict(RANGE)}, headers=headers
            )
            assert response.status_code == 404, code
            assert client.get(f"/api/v1/reports/{code}/params", headers=headers).status_code == 404


class TestMidYearOpeningBalances:
    def test_books_carry_ytd_movement_into_the_opening_column(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        print_viewer_role: str,
        test_password: str,
        print_context: PostingContext,
        print_context_b: PostingContext,
    ) -> None:
        """M1 (review 5D): nhánh cộng phát-sinh-đầu-năm→trước-`from_date` phải
        có test ghim SỐ DƯƠNG — from=01/02: dư đầu 111 = 1.400.000 (thu tháng 1
        của cả hai chi nhánh), S06 dư đầu cân hai vế."""
        _seed_local_books(session_factory, print_dataset, print_context, print_context_b)
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            print_viewer_role,
            "giua-nam",
            test_password,
            print_context.branch_id,
        )
        mid_year = {"from_date": "2026-02-01", "to_date": "2026-02-28"}

        detail = _preview(client, headers, "S38-DN", {**mid_year, "account_code": "111"})
        rows = _data_rows(detail)
        assert _cell(detail, rows[0], "description") == "Số dư đầu kỳ"
        assert _cell(detail, rows[0], "balance_debit") == "1.400.000"
        assert _cell(detail, rows[-1], "balance_debit") == "1.200.000"

        trial = _preview(client, headers, "S06-DN", mid_year)
        by_account = {_cell(trial, row, "account_code"): row for row in _data_rows(trial)}
        assert _cell(trial, by_account["111"], "opening_debit") == "1.400.000"
        assert _cell(trial, by_account["511"], "opening_credit") == "1.400.000"
        assert _cell(trial, by_account["111"], "closing_debit") == "1.200.000"


class TestPrintGuards:
    def test_a_cancelled_voucher_refuses_to_print(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        printer_role: str,
        test_password: str,
        print_context: PostingContext,
    ) -> None:
        """M2 (review 5D): nhánh DA_HUY của `_guard_printable` phải có test.

        Chưa có đường service nào hủy chứng từ (phase 7) — set thẳng status,
        đúng cách probe của review làm."""
        from sqlalchemy import update as sql_update

        from ket.posting.contracts import Voucher, VoucherStatus

        voucher_id = TestVoucherPrinting()._make_voucher(
            session_factory, print_dataset, print_context, amount=11_000, post=False
        )
        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            session.execute(
                sql_update(Voucher)
                .where(Voucher.id == voucher_id)
                .values(status=VoucherStatus.DA_HUY)
            )
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            printer_role,
            "in-huy",
            test_password,
            print_context.branch_id,
        )
        blocked = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "print.not_allowed"

    def test_the_locked_period_switch_blocks_and_releases(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        printer_role: str,
        test_password: str,
        print_context: PostingContext,
    ) -> None:
        """M2 (review 5D): công tắc `print.allow_locked_vouchers` — kỳ khóa +
        công tắc tắt → 409; bật lại (mặc định) → in được."""
        from sqlalchemy import update as sql_update

        from ket.kernel.config.catalog import PRINT_ALLOW_LOCKED_KEY
        from ket.kernel.periods.models import AccountingPeriod
        from ket.posting.contracts import Voucher

        voucher_id = TestVoucherPrinting()._make_voucher(
            session_factory, print_dataset, print_context, amount=22_000, post=True
        )
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            printer_role,
            "in-khoa",
            test_password,
            print_context.branch_id,
        )
        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            period_id = session.execute(
                select(Voucher.period_id).where(Voucher.id == voucher_id)
            ).scalar_one()
            # Khóa kỳ bằng dữ liệu (đường service khóa tuần tự là việc của 4D,
            # không phải điều test này đo) + tắt công tắc.
            session.execute(
                sql_update(AccountingPeriod)
                .where(AccountingPeriod.id == period_id)
                .values(locked_at=datetime.now(UTC), locked_by=ACTOR_ID)
            )
            set_setting(
                session,
                key=PRINT_ALLOW_LOCKED_KEY,
                scope=SettingScope.SYSTEM,
                user_id=ACTOR_ID,
                raw_value="false",
                expected_row_version=None,
            )
        try:
            blocked = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
            assert blocked.status_code == 409
            assert blocked.json()["error_code"] == "print.not_allowed"
        finally:
            with unit_of_work(
                session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
            ) as session:
                set_setting(
                    session,
                    key=PRINT_ALLOW_LOCKED_KEY,
                    scope=SettingScope.SYSTEM,
                    user_id=ACTOR_ID,
                    raw_value="true",
                    expected_row_version=1,
                )
        allowed = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
        assert allowed.status_code == 200
        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            session.execute(
                sql_update(AccountingPeriod)
                .where(AccountingPeriod.id == period_id)
                .values(locked_at=None, locked_by=None)
            )

    def test_two_concurrent_prints_get_consecutive_copy_numbers(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        user_factory: UserFactory,
        printer_role: str,
        test_password: str,
        print_context: PostingContext,
    ) -> None:
        """M5 (review 5D, họ RT-09): FOR UPDATE trên dòng chứng từ là cơ chế
        trung tâm của `copy_no` — hai lượt in đồng thời phải ra hai số nối
        tiếp, không trùng."""
        from concurrent.futures import ThreadPoolExecutor

        voucher_id = TestVoucherPrinting()._make_voucher(
            session_factory, print_dataset, print_context, amount=33_000, post=True
        )
        headers = _headers(
            client,
            session_factory,
            print_dataset,
            user_factory,
            printer_role,
            "in-dua",
            test_password,
            print_context.branch_id,
        )

        def do_print() -> tuple[int, str]:
            response = client.post(f"/api/v1/vouchers/{voucher_id}/print", json={}, headers=headers)
            return response.status_code, response.headers.get("X-Print-Copy-No", "")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _i: do_print(), range(2)))
        assert {first[0], second[0]} == {200}
        assert sorted([first[1], second[1]]) == ["1", "2"]


class TestRefreshPreservesRuntimeRows:
    def test_refresh_builtin_reports_leaves_non_builtin_definitions_alone(
        self,
        owner_engine: Engine,
        session_factory: sessionmaker[Session],
        print_dataset: DatasetRef,
        print_context: PostingContext,
    ) -> None:
        """M4 (review 5D): hàng rào "chỉ xóa builtin" của `refresh` phải có
        test đỏ được — không chỉ một dòng docstring."""
        from ket.kernel.config.reports.models import ReportDefinition
        from ket.kernel.config.reports.seed import refresh_builtin_reports

        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            session.add(
                ReportDefinition(
                    code="BAO-CAO-RIENG-5D",
                    name="Báo cáo người dùng đăng ký",
                    category="noi-bo",
                    module="general_ledger",
                    dataset_code="gl_ledger",
                    layout_code="gl-ledger",
                    param_set_code="gl_ledger_params",
                    ledger_scope="both",
                    is_builtin=False,
                    package_id=None,
                )
            )
        with owner_engine.begin() as connection:
            refresh_builtin_reports(connection, print_dataset.schema_name)
        with unit_of_work(
            session_factory, posting_scope(print_dataset, print_context, user_id=ACTOR_ID)
        ) as session:
            codes = set(session.scalars(select(ReportDefinition.code)))
            assert "BAO-CAO-RIENG-5D" in codes
            assert {"S03a-DN", "S06-DN", "F01-DNN"} <= codes
