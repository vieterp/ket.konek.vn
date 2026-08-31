"""Bộ báo cáo phân hệ Quỹ & Ngân hàng (bước 17 phase-06, lát 6E-1).

Tất cả đi qua chính đường preview của report engine — bằng chứng cho tiêu chí
"đăng ký hoàn toàn bằng metadata, 0 dòng code renderer riêng". Bốn nhóm bất
biến:

* hai mẫu sổ chỉ khác một tham số GHIM dùng chung một dataset và vẫn ra hai tờ
  giấy khác nhau (`S07a-DN`/`S08-DN`, `S03a1-DN`/`S03a2-DN`);
* sổ quỹ THỦ QUỸ (`S07-DN`) đọc `treasurer_cash_book`, không đọc `gl_postings`
  — nếu gộp nguồn thì báo cáo chênh lệch sẽ luôn ra 0 dòng;
* báo cáo chênh lệch thấy được chênh lệch **hợp lệ về nghiệp vụ** (phiếu kế
  toán đã ghi sổ, thủ quỹ chưa ghi sổ quỹ) — thứ mà check toàn vẹn cố ý im
  lặng;
* `bang-ke-so-du-ngan-hang`: cuối kỳ = đầu kỳ + thu − chi, trên từng TK.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from catalog_api_support import UserFactory, actor, all_branch_codes, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.catalog import TREASURER_ENABLED_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Setting
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.bank.models import BankStatement, BankStatementLine, BankVoucherKind
from ket.modules.bank.reconciliation import match_line
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.warehousing.treasurer.queue_service import book_vouchers
from ket.settings import Settings
from posting_support import (
    PostingContext,
    ensure_second_branch,
    posting_scope,
    seed_posting_context,
)

pytestmark = pytest.mark.db

ACTOR_ID = 1
REPORT_ROLE = "xem_so_quy_ngan_hang"

# Cửa sổ riêng của tệp này (tháng 10) — `dataset_alpha` dùng chung, tách dữ
# liệu bằng THỜI GIAN chứ không bằng thứ tự chạy (bài học 5D).
OCT_05 = date(2026, 10, 5)
OCT_06 = date(2026, 10, 6)
OCT_31 = date(2026, 10, 31)
# Cặp khớp bắc qua ranh giới kỳ (H-2): chứng từ cuối tháng 11, sao kê về đầu
# tháng 12 — đúng cửa sổ ±3 ngày mà auto-match của 6D chấp nhận.
EDGE_VOUCHER_DATE = date(2026, 11, 29)
EDGE_STATEMENT_DATE = date(2026, 12, 2)
BANK_ACCOUNT_CODE = "RPT-BOOK-001"

RECEIPT_AMOUNT = Decimal("520000")
PAYMENT_AMOUNT = Decimal("310000")
ADVICE_AMOUNT = Decimal("770000")
UNMATCHED_STATEMENT_AMOUNT = Decimal("125000")


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
    codes = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return codes


@pytest.fixture(scope="module")
def bank_account_id(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code=BANK_ACCOUNT_CODE
    )


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
) -> dict[str, UUID]:
    """Phiếu thu + phiếu chi quỹ, một giấy báo có, và một sao kê hai dòng.

    Phân hệ thủ quỹ **bật** trong lúc gieo để phiếu nằm lại hàng đợi (chưa vào
    sổ quỹ) — đó là nguyên liệu của báo cáo chênh lệch. Trả về `false` sau khi
    gieo xong để tệp test khác không thấy trạng thái lạ (khuôn `test_treasurer_
    queue.py`).
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        _set_treasurer_enabled(session, enabled=True)
        cash = CashVoucherService(session)
        receipt = cash.create(
            _cash_payload(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation_code="thu-khac",
                debit=accounts["111"],
                credit=accounts["3381"],
                amount=RECEIPT_AMOUNT,
                description="Sổ quỹ — thu",
            ),
            user_id=ACTOR_ID,
        )
        cash.post(receipt.id, user_id=ACTOR_ID)
        payment = cash.create(
            _cash_payload(
                context,
                accounts,
                kind=CashVoucherKind.PAYMENT,
                operation_code="chi-khac",
                debit=accounts["642"],
                credit=accounts["111"],
                amount=PAYMENT_AMOUNT,
                description="Sổ quỹ — chi",
            ),
            user_id=ACTOR_ID,
        )
        cash.post(payment.id, user_id=ACTOR_ID)
        ids["receipt"] = receipt.id
        ids["payment"] = payment.id

        # Chỉ phiếu THU được thủ quỹ ghi sổ quỹ; phiếu CHI ở lại hàng đợi để
        # báo cáo chênh lệch có đúng một dòng biết trước.
        # Dữ liệu dựng ở ngày cố định tháng 10/2026 — tiêm đồng hồ ĐÚNG ngày đó:
        # trần "sổ quỹ không ghi tương lai" so với đồng hồ thật sẽ từ chối oan
        # (và test này phải chạy được ở mọi năm sau).
        book_vouchers(
            session,
            voucher_ids=[receipt.id],
            book_date=None,
            user_id=ACTOR_ID,
            today=OCT_05,
        )

        bank = BankVoucherService(session)
        advice = bank.create(
            BankVoucherIn(
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation_code="thu-khac",
                bank_account_id=bank_account_id,
                branch_id=context.branch_id,
                document_date=OCT_05,
                posting_date=OCT_05,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="Báo có sổ tiền gửi",
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["3381"],
                        amount_fc=ADVICE_AMOUNT,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        bank.post(advice.id, user_id=ACTOR_ID)
        ids["advice"] = advice.id

        statement = BankStatement(
            bank_account_id=bank_account_id,
            statement_date=OCT_06,
            imported_by=ACTOR_ID,
        )
        session.add(statement)
        session.flush()
        matched_line = BankStatementLine(
            statement_id=statement.id,
            bank_account_id=bank_account_id,
            line_no=1,
            txn_date=OCT_05,
            reference_no="SK-KHOP",
            description="Dòng sao kê khớp báo có",
            debit=Decimal(0),
            credit=ADVICE_AMOUNT,
        )
        orphan_line = BankStatementLine(
            statement_id=statement.id,
            bank_account_id=bank_account_id,
            line_no=2,
            txn_date=OCT_06,
            reference_no="SK-LE",
            description="Dòng sao kê chưa có chứng từ",
            debit=Decimal(0),
            credit=UNMATCHED_STATEMENT_AMOUNT,
        )
        session.add_all([matched_line, orphan_line])
        session.flush()
        match_line(session, line_id=matched_line.id, voucher_id=advice.id)
        _set_treasurer_enabled(session, enabled=False)
    return ids


def _set_treasurer_enabled(session: Session, *, enabled: bool) -> None:
    value = "true" if enabled else "false"
    row = session.scalar(
        select(Setting).where(Setting.key == TREASURER_ENABLED_KEY, Setting.scope == "system")
    )
    if row is None:
        session.add(
            Setting(scope="system", key=TREASURER_ENABLED_KEY, value=value, value_type="boolean")
        )
    else:
        row.value = value
    session.flush()


def _cash_payload(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    kind: int,
    operation_code: str,
    debit: int,
    credit: int,
    amount: Decimal,
    description: str,
) -> CashVoucherIn:
    return CashVoucherIn(
        kind=kind,
        operation_code=operation_code,
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=OCT_05,
        posting_date=OCT_05,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description=description,
        lines=(
            CashVoucherLineIn(debit_account_id=debit, credit_account_id=credit, amount_fc=amount),
        ),
    )


@pytest.fixture(scope="module")
def report_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Quyền báo cáo **và** quyền xem dữ liệu ba phân hệ liên quan.

    Trước bản vá H-1b một mình `reporting.report.view` mở được cả sổ quỹ lẫn
    bảng đối chiếu ngân hàng — chính fixture cũ là bằng chứng của lỗ hổng.
    """
    return ensure_role(
        session_factory,
        dataset_alpha,
        REPORT_ROLE,
        [
            "reporting.report.view",
            permission_code("cash_book", "receipt", Action.VIEW),
            permission_code("bank", "credit_advice", Action.VIEW),
            permission_code("treasurer", "cash_book", Action.VIEW),
        ],
    )


@pytest.fixture
def headers_for_reports(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    report_role: str,
) -> dict[str, str]:
    return {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            report_role,
            "bao_cao_params",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }


Preview = Callable[..., "PreviewResult"]


class PreviewResult:
    """Kết quả một lượt xem trước, tra được theo KHÓA CỘT.

    Ô trong `cells` xếp đúng thứ tự `columns`, nên tra theo khóa thay vì cắt
    chuỗi: khẳng định "cột `closing` bằng cột `opening` cộng phát sinh" phải
    nói về đúng cột đó, không về ô thứ tám của một dòng đã định dạng.
    """

    def __init__(self, body: dict[str, object]) -> None:
        columns = body["columns"]
        assert isinstance(columns, list)
        self.column_keys = [str(column["key"]) for column in columns]
        rows = body["rows"]
        assert isinstance(rows, list)
        self.rows: list[dict[str, str]] = []
        self.headings: list[str] = []
        for row in rows:
            if row["kind"] in ("group_header", "group_footer") and row.get("heading"):
                self.headings.append(str(row["heading"]))
            cells = row.get("cells")
            if row["kind"] != "data" or not cells:
                continue
            self.rows.append(
                {key: str(cell["text"]) for key, cell in zip(self.column_keys, cells, strict=False)}
            )

    def texts(self) -> list[str]:
        """Mỗi dòng dữ liệu ghép thành một chuỗi — cho khẳng định "có/không có"."""
        return ["|".join(row.values()) for row in self.rows]

    def all_text(self) -> str:
        """Cả tiêu đề nhóm lẫn dòng dữ liệu — chiều gộp (tài khoản ngân hàng,
        số hiệu TK) nằm trên TIÊU ĐỀ nhóm, không lặp lại ở từng dòng."""
        return "\n".join([*self.headings, *self.texts()])

    def money(self, row: dict[str, str], key: str) -> Decimal:
        """Ô tiền đã định dạng → `Decimal`. Ô rỗng = 0 (renderer bỏ trắng số 0)."""
        text = row[key].strip().replace("\u00a0", "").replace(".", "").replace(",", "")
        if text in ("", "-"):
            return Decimal(0)
        if text.startswith("(") and text.endswith(")"):
            text = f"-{text[1:-1]}"
        return Decimal(text)


@pytest.fixture
def preview(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    report_role: str,
) -> Preview:
    """Render một báo cáo và trả về từng dòng dữ liệu đã ghép thành chuỗi."""
    headers = {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            report_role,
            "so_quy",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": OCT_05.isoformat(),
                "to_date": OCT_31.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


@pytest.fixture
def preview_company_wide(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    report_role: str,
) -> Preview:
    """`preview` với phạm vi TOÀN ĐƠN VỊ — bắt buộc cho `doi-chieu-ngan-hang`.

    Báo cáo ấy bật `requires_full_branch_scope` từ lát 6G-2 (M-4): nó so sao kê
    (mức tài khoản, không mang chi nhánh) với sổ (dưới RLS chi nhánh), nên đọc
    dưới phạm vi hẹp cho ra phần lệch lớn hơn thật. Cổng trả 403, và bài kiểm
    phải đi qua cổng bằng phạm vi đúng chứ không bằng cách gỡ cổng.

    `ensure_second_branch` gọi tường minh: `dataset_alpha` dùng chung, chạy
    RIÊNG tệp này thì có thể chỉ một chi nhánh — lúc đó "hẹp" và "toàn đơn vị"
    là cùng một tập và bài kiểm xanh vì lý do khác lý do nó viết ra.
    """
    ensure_second_branch(session_factory, dataset_alpha)
    headers = {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            report_role,
            "doi_chieu_toan_don_vi",
            test_password,
            branch_codes=all_branch_codes(session_factory, dataset_alpha),
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": OCT_05.isoformat(),
                "to_date": OCT_31.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


class TestPinnedFormsShareADataset:
    def test_cash_and_deposit_detail_books_show_only_their_own_account_group(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        cash_book = preview("S07a-DN").all_text()
        deposit_book = preview("S08-DN").all_text()
        # `account_prefix` ghim 111 vs 112 — cùng một câu SQL, hai tờ giấy.
        assert "Sổ quỹ — thu" in cash_book
        assert "Báo có sổ tiền gửi" not in cash_book
        assert "Báo có sổ tiền gửi" in deposit_book
        assert "Sổ quỹ — thu" not in deposit_book

    def test_the_deposit_book_names_the_bank_account_it_belongs_to(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        # S08-DN là sổ của MỘT tài khoản tiền gửi; chiều gộp đó phải có thật
        # trong dữ liệu, nếu không mẫu sổ mất tiêu đề "Nơi mở tài khoản".
        assert BANK_ACCOUNT_CODE in preview("S08-DN").all_text()

    def test_cash_journals_split_by_the_pinned_direction(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        receipts = preview("S03a1-DN")
        payments = preview("S03a2-DN")
        assert "Sổ quỹ — thu" in receipts.all_text()
        assert "Sổ quỹ — chi" not in receipts.all_text()
        assert "Sổ quỹ — chi" in payments.all_text()
        assert "Sổ quỹ — thu" not in payments.all_text()

        # Cột tiền phải mang ĐÚNG chiều của sổ (review 6E-1 L-3: đột biến "luôn
        # lấy `p.debit`" sống — Sổ Nhật ký chi tiền sẽ in toàn số 0 mà test vẫn
        # xanh). Nhật ký thu đọc bên Nợ TK tiền, nhật ký chi đọc bên Có.
        receipt_row = next(row for row in receipts.rows if row["description"] == "Sổ quỹ — thu")
        assert receipts.money(receipt_row, "money_amount") == RECEIPT_AMOUNT
        assert receipts.money(receipt_row, "other_amount") == RECEIPT_AMOUNT
        assert receipt_row["money_account_code"] == "111"

        payment_row = next(row for row in payments.rows if row["description"] == "Sổ quỹ — chi")
        assert payments.money(payment_row, "money_amount") == PAYMENT_AMOUNT

    def test_a_pinned_param_cannot_be_overridden_from_the_request(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        report_role: str,
    ) -> None:
        """Gửi `direction=chi` cho Sổ Nhật ký THU phải bị từ chối tường minh —
        không được lặng lẽ đổi nội dung một mẫu sổ pháp lý."""
        headers = {
            **actor(
                client,
                session_factory,
                dataset_alpha,
                user_factory,
                report_role,
                "so_quy_ghim",
                test_password,
                branch_codes=[context.branch_code],
            ),
            BRANCH_HEADER: str(context.branch_id),
        }
        response = client.post(
            "/api/v1/reports/S03a1-DN/preview",
            json={
                "params": {
                    "from_date": OCT_05.isoformat(),
                    "to_date": OCT_31.isoformat(),
                    "direction": "chi",
                }
            },
            headers=headers,
        )
        assert response.status_code == 422, response.text


class TestModulePermissionGate:
    """Review 6E-1 H-1b — `reporting.report.view` trả lời "được dùng chức năng
    báo cáo không", KHÔNG trả lời "được đọc dữ liệu phân hệ này không"."""

    def test_report_view_alone_cannot_open_a_module_report(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        books: dict[str, UUID],
    ) -> None:
        bare = ensure_role(
            session_factory, dataset_alpha, "chi_xem_bao_cao", ["reporting.report.view"]
        )
        headers = {
            **actor(
                client,
                session_factory,
                dataset_alpha,
                user_factory,
                bare,
                "bao_cao_tran",
                test_password,
                branch_codes=[context.branch_code],
            ),
            BRANCH_HEADER: str(context.branch_id),
        }
        body = {"params": {"from_date": OCT_05.isoformat(), "to_date": OCT_31.isoformat()}}
        for code in ("doi-chieu-ngan-hang", "S07a-DN", "S07-DN"):
            refused = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
            assert refused.status_code == 403, f"{code}: {refused.text}"

        # Bộ sổ tổng hợp đóng cổng `general_ledger` từ lát 6G-1: nó đọc
        # `gl_postings` của MỌI phân hệ, nên "được dùng chức năng báo cáo" một
        # mình là cổng lỏng nhất trong cả danh mục.
        refused_ledger = client.post("/api/v1/reports/S03a-DN/preview", json=body, headers=headers)
        assert refused_ledger.status_code == 403, refused_ledger.text

        # Và danh mục không mời người ta bấm vào một 403.
        listed = client.get("/api/v1/reports", headers=headers)
        assert listed.status_code == 200
        codes = {item["code"] for item in listed.json()["reports"]}
        assert "doi-chieu-ngan-hang" not in codes
        assert "S03a-DN" not in codes

    def test_a_pinned_param_is_not_offered_as_an_input_field(
        self, client: TestClient, headers_for_reports: dict[str, str]
    ) -> None:
        """Review 6E-1 M-2: client dựng form từ `/params`; một ô "Chiều tiền"
        bắt buộc cho Sổ Nhật ký THU là ô mà mọi giá trị khác `thu` đều 422."""
        response = client.get("/api/v1/reports/S03a1-DN/params", headers=headers_for_reports)
        assert response.status_code == 200, response.text
        assert [param["name"] for param in response.json()["params"]] == []

        deposit = client.get("/api/v1/reports/S07a-DN/params", headers=headers_for_reports)
        assert deposit.status_code == 200
        names = [param["name"] for param in deposit.json()["params"]]
        assert "account_prefix" not in names, "tham số ghim không phải ô nhập"
        assert "account_code" in names, "tham số thật vẫn phải còn"


class TestTreasurerBooks:
    def test_the_treasurer_book_holds_only_what_the_treasurer_booked(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`S07-DN` đọc sổ quỹ thủ quỹ: phiếu thu đã ghi sổ quỹ có mặt, phiếu
        chi còn trong hàng đợi thì KHÔNG — đó là sự khác biệt với `S07a-DN`."""
        rows = preview("S07-DN").all_text()
        assert "Sổ quỹ — thu" in rows
        assert "Sổ quỹ — chi" not in rows

    def test_the_difference_report_surfaces_the_unbooked_voucher(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        result = preview("chenh-lech-so-quy-so-ke-toan")
        payment_rows = [row for row in result.rows if row["description"] == "Sổ quỹ — chi"]
        assert payment_rows, "phiếu chi chưa ghi sổ quỹ phải hiện ra"
        assert "thủ quỹ chưa ghi sổ quỹ" in payment_rows[0]["reason"]
        # Phiếu CHI làm sổ kế toán giảm (`ledger_net` âm) trong khi sổ quỹ chưa
        # nhúc nhích → chênh lệch = ledger − book = ÂM đúng bằng số tiền phiếu.
        assert result.money(payment_rows[0], "ledger_net") == -PAYMENT_AMOUNT
        assert result.money(payment_rows[0], "book_net") == Decimal(0)
        assert result.money(payment_rows[0], "difference") == -PAYMENT_AMOUNT
        # Phiếu đã khớp trọn vẹn không thuộc báo cáo chênh lệch.
        assert not [row for row in result.rows if row["description"] == "Sổ quỹ — thu"]

    def test_a_draft_voucher_never_enters_the_difference_report(
        self,
        preview: Preview,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
        books: dict[str, UUID],
    ) -> None:
        """Review 6E-1 H-4: phiếu mới lập chưa ghi sổ từng lọt vào đây kèm lý do
        "Kế toán đã ghi sổ, thủ quỹ chưa ghi sổ quỹ" — khẳng định sai một việc
        chưa xảy ra, và đọc như một lời buộc tội thủ quỹ."""
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            draft = CashVoucherService(session).create(
                _cash_payload(
                    context,
                    accounts,
                    kind=CashVoucherKind.PAYMENT,
                    operation_code="chi-khac",
                    debit=accounts["642"],
                    credit=accounts["111"],
                    amount=Decimal("999000"),
                    description="Phiếu nháp chưa ghi sổ",
                ),
                user_id=ACTOR_ID,
            )
        rows = preview("chenh-lech-so-quy-so-ke-toan").rows
        assert draft.voucher_no not in {row["voucher_no"] for row in rows}
        assert all(row["description"] != "Phiếu nháp chưa ghi sổ" for row in rows)


class TestBankBooks:
    def test_reconciliation_separates_matched_and_both_kinds_of_gap(
        self, preview_company_wide: Preview, books: dict[str, UUID]
    ) -> None:
        result = preview_company_wide("doi-chieu-ngan-hang")
        by_reference = {row["statement_reference"]: row for row in result.rows}

        matched = by_reference["SK-KHOP"]
        assert matched["status"] == "Đã khớp"
        # Hai vế cùng số tiền → lệch 0. Đây là ô mà quy ước dấu sai sẽ làm nó
        # bằng HAI LẦN số tiền thay vì 0.
        assert result.money(matched, "difference") == Decimal(0)
        assert result.money(matched, "statement_receipt") == ADVICE_AMOUNT
        assert result.money(matched, "voucher_net") == ADVICE_AMOUNT

        orphan = by_reference["SK-LE"]
        assert orphan["status"] == "Sao kê chưa có chứng từ"
        assert result.money(orphan, "difference") == UNMATCHED_STATEMENT_AMOUNT
        assert not orphan["voucher_no"]

    def test_a_matched_pair_across_the_period_edge_makes_no_phantom_difference(
        self,
        preview_company_wide: Preview,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
        bank_account_id: int,
        books: dict[str, UUID],
    ) -> None:
        """Review 6E-1 H-2: auto-match của 6D cố ý chấp nhận lệch ±3 ngày, nên
        cặp đã khớp bắc qua ngày cuối kỳ là chuyện thường tháng. Cắt ngày ở vế
        đối ứng làm chính cặp đó hiện thành HAI dòng lệch giả — mỗi kỳ một dòng
        — mà tổng cả năm vẫn đúng nên không ai tra ra."""
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        amount = Decimal("444000")
        with unit_of_work(session_factory, scope) as session:
            bank = BankVoucherService(session)
            advice = bank.create(
                BankVoucherIn(
                    kind=BankVoucherKind.CREDIT_ADVICE,
                    operation_code="thu-khac",
                    bank_account_id=bank_account_id,
                    branch_id=context.branch_id,
                    document_date=EDGE_VOUCHER_DATE,
                    posting_date=EDGE_VOUCHER_DATE,
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    description="Báo có bắc qua ranh giới kỳ",
                    lines=(
                        BankVoucherLineIn(
                            debit_account_id=accounts["112"],
                            credit_account_id=accounts["3381"],
                            amount_fc=amount,
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
            bank.post(advice.id, user_id=ACTOR_ID)
            statement = BankStatement(
                bank_account_id=bank_account_id,
                statement_date=EDGE_STATEMENT_DATE,
                imported_by=ACTOR_ID,
            )
            session.add(statement)
            session.flush()
            line = BankStatementLine(
                statement_id=statement.id,
                bank_account_id=bank_account_id,
                line_no=1,
                txn_date=EDGE_STATEMENT_DATE,
                reference_no="SK-BIENKY",
                description="Dòng sao kê về muộn 3 ngày",
                debit=Decimal(0),
                credit=amount,
            )
            session.add(line)
            session.flush()
            match_line(session, line_id=line.id, voucher_id=advice.id)

        # Kỳ chỉ chứa DÒNG SAO KÊ (chứng từ nằm ở kỳ trước).
        statement_side = preview_company_wide(
            "doi-chieu-ngan-hang",
            from_date=EDGE_STATEMENT_DATE.isoformat(),
            to_date=(EDGE_STATEMENT_DATE + timedelta(days=5)).isoformat(),
        )
        matched = [row for row in statement_side.rows if row["statement_reference"] == "SK-BIENKY"]
        assert matched, "dòng sao kê phải có mặt trong kỳ chứa nó"
        assert matched[0]["status"] == "Đã khớp"
        assert statement_side.money(matched[0], "difference") == Decimal(0)

        # Kỳ chỉ chứa CHỨNG TỪ (dòng sao kê về muộn, nằm ở kỳ sau).
        voucher_side = preview_company_wide(
            "doi-chieu-ngan-hang",
            from_date=(EDGE_VOUCHER_DATE - timedelta(days=1)).isoformat(),
            to_date=EDGE_VOUCHER_DATE.isoformat(),
        )
        assert advice.voucher_no not in {row["voucher_no"] for row in voucher_side.rows}, (
            "chứng từ ĐÃ khớp không phải 'chứng từ chưa có trên sao kê'"
        )

    def test_balance_summary_closes_with_opening_plus_movement(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        result = preview("bang-ke-so-du-ngan-hang")
        rows = [row for row in result.rows if row["bank_account_code"] == BANK_ACCOUNT_CODE]
        assert rows, "tài khoản ngân hàng của tệp test phải có mặt"
        row = rows[0]
        opening = result.money(row, "opening")
        receipt = result.money(row, "receipt")
        payment = result.money(row, "payment")
        closing = result.money(row, "closing")
        assert closing == opening + receipt - payment
        # Không chỉ là hằng đúng theo cấu tạo (review 6E-1: xóa nhánh dư đầu năm
        # mà test vẫn xanh): ghim CHÍNH con số phát sinh và số dư cuối kỳ.
        assert receipt == ADVICE_AMOUNT
        assert closing == opening + ADVICE_AMOUNT
