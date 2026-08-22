"""In chứng từ tiền qua HTTP trên PostgreSQL thật — lát 6E-2.

Kiểm cái mà chỉ đường THẬT lộ ra: hook `print_details` của registry có được
gọi không, trường của module có tới được tờ giấy không, biên bản kiểm kê in
được không, và ai in được cái gì. Bố cục tờ giấy kiểm ở
`test_money_words_and_print_forms.py`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.formatting import format_money
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

PRINTER_ROLE = "ke_toan_in_chung_tu"
CUSTOMER_ID = 9401
PDF_MEDIA_TYPE = "application/pdf"


def _printer_codes() -> list[str]:
    codes = [
        permission_code("cash_book", name, action)
        for name in ("receipt", "payment")
        for action in (Action.VIEW, Action.CREATE, Action.PRINT, Action.POST)
    ]
    codes += [
        permission_code("bank", name, action)
        for name in ("credit_advice", "payment_order")
        for action in (Action.VIEW, Action.CREATE, Action.PRINT)
    ]
    codes += [
        permission_code("cash_book", "count_sheet", action)
        for action in (Action.VIEW, Action.CREATE)
    ]
    return codes


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
        session_factory, dataset_alpha, context, code="0031-BANK-PRINT"
    )


@pytest.fixture(scope="module")
def customer(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    """Khách hàng có ĐỊA CHỈ — ô "Địa chỉ" của 01-TT lấy từ danh mục đối tác."""
    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        existing = session.get(Partner, CUSTOMER_ID)
        if existing is None:
            session.add(
                Partner(
                    id=CUSTOMER_ID,
                    code="KH-IN-01",
                    name="Công ty CP Sao Mai",
                    path=f"{CUSTOMER_ID}.",
                    is_customer=True,
                    address="88 Nguyễn Huệ, TP Huế",
                )
            )
            session.flush()
    return CUSTOMER_ID


@pytest.fixture
def printer_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    role = ensure_role(session_factory, dataset_alpha, PRINTER_ROLE, _printer_codes())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "print_author",
        test_password,
        branch_codes=[context.branch_code],
    )


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def _post(
    client: TestClient, headers: dict[str, str], path: str, body: dict[str, object]
) -> dict[str, str]:
    """Tạo một bản ghi rồi trả thân JSON — mọi endpoint dùng ở đây đều đòi
    khóa idempotency, và test không quan tâm 200 (phát lại) hay 201."""
    response = client.post(path, json=body, headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex})
    assert response.status_code in (200, 201), response.text
    created: dict[str, str] = response.json()
    return created


def test_receipt_print_carries_the_module_fields_onto_form_01tt(
    client: TestClient,
    printer_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
    customer: int,
) -> None:
    """Hook `print_details` chạy trên đường in dùng chung: tên người nộp, địa
    chỉ đối tác, lý do, "kèm theo", cặp Nợ/Có và số tiền bằng chữ đều là thứ
    chỉ module quỹ biết, mà `routers/printing.py` không có dòng nào nhắc "PT".
    """
    voucher = _post(
        client,
        printer_headers,
        "/api/v1/cash-book/vouchers",
        {
            "kind": 0,
            "operation_code": "thu-no-khach-hang",
            "cash_account_id": accounts["111"],
            "branch_id": context.branch_id,
            "document_date": "2026-02-10",
            "posting_date": "2026-02-10",
            "currency_code": "VND",
            "exchange_rate": "1",
            "description": "thu tiền hàng tháng 1",
            "partner_kind": 0,
            "partner_id": customer,
            "payer_receiver_name": "Trần Thị Bích",
            "attachment_count": 2,
            "lines": [
                {
                    "debit_account_id": accounts["111"],
                    "credit_account_id": accounts["131"],
                    "amount_fc": "5200000",
                    "partner_kind": 0,
                    "partner_id": customer,
                }
            ],
        },
    )
    response = client.post(
        f"/api/v1/vouchers/{voucher['id']}/print", json={}, headers=printer_headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == PDF_MEDIA_TYPE
    assert response.headers["X-Print-Copy-No"] == "1"

    text = _pdf_text(response.content)
    assert "Mẫu số 01 - TT" in text
    assert "Trần Thị Bích" in text
    assert "88 Nguyễn Huệ, TP Huế" in text
    assert "thu tiền hàng tháng 1" in text
    assert "2 chứng từ gốc" in text
    assert "Năm triệu hai trăm nghìn đồng chẵn" in text
    assert voucher["voucher_no"] in text
    # Chứng từ chưa ghi sổ vẫn in được, nhưng mang dấu (FR-RPT-011).
    assert "BẢN NHÁP" in text


def test_payment_order_print_shows_the_partner_bank_account(
    client: TestClient,
    printer_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
    vnd_account: int,
) -> None:
    """§Success Criteria: "Ủy nhiệm chi in ra lấy đúng TK ngân hàng của đối
    tác (FR-SYS-033)" — số tài khoản người thụ hưởng phải có mặt trên tờ giấy,
    không chỉ trong DB."""
    voucher = _post(
        client,
        printer_headers,
        "/api/v1/bank/vouchers",
        {
            "kind": 1,
            "operation_code": "chi-khac",
            "bank_account_id": vnd_account,
            "branch_id": context.branch_id,
            "document_date": "2026-02-12",
            "posting_date": "2026-02-12",
            "currency_code": "VND",
            "exchange_rate": "1",
            "description": "thanh toán hóa đơn 0001234",
            "beneficiary_name": "Công ty TNHH Vật tư Sao Mai",
            "beneficiary_account_no": "19001199887766",
            "beneficiary_bank_name": "Techcombank",
            "lines": [
                {
                    "debit_account_id": accounts["3381"],
                    "credit_account_id": accounts["112"],
                    "amount_fc": "88000000",
                }
            ],
        },
    )
    response = client.post(
        f"/api/v1/vouchers/{voucher['id']}/print", json={}, headers=printer_headers
    )
    assert response.status_code == 200, response.text

    text = _pdf_text(response.content)
    assert "19001199887766" in text
    assert "Công ty TNHH Vật tư Sao Mai" in text
    assert "0031-BANK-PRINT" in text  # số tài khoản của chính đơn vị
    assert "Tám mươi tám triệu đồng chẵn" in text
    # Thông tư không có mẫu ủy nhiệm chi — bản in nội bộ không mượn số hiệu mẫu.
    assert "Mẫu số" not in text


def test_count_sheet_print_follows_form_08att_sign_direction(
    client: TestClient,
    printer_headers: dict[str, str],
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Mẫu 08a-TT định nghĩa `III = I − II` (số sổ − thực tế): đếm THIẾU thì
    dòng III **dương**. Service xử lý chênh lệch tính ngược chiều (`thực tế −
    sổ`, dương = thừa) vì nó cần dấu theo chiều bút toán — in nhầm chiều là
    biên bản báo thừa đúng lúc két thiếu.
    """
    receipt = _post(
        client,
        printer_headers,
        "/api/v1/cash-book/vouchers",
        {
            "kind": 0,
            "operation_code": "thu-khac",
            "cash_account_id": accounts["111"],
            "branch_id": context.branch_id,
            "document_date": "2026-03-05",
            "posting_date": "2026-03-05",
            "currency_code": "VND",
            "exchange_rate": "1",
            "lines": [
                {
                    "debit_account_id": accounts["111"],
                    "credit_account_id": accounts["3381"],
                    "amount_fc": "500000",
                }
            ],
        },
    )
    posted = client.post(
        f"/api/v1/vouchers/{receipt['id']}/actions/post",
        json={},
        headers={**printer_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert posted.status_code == 200, posted.text

    # Số dư sổ của TK quỹ là dữ liệu DÙNG CHUNG với các tệp test khác trên
    # `dataset_alpha`; đọc nó ra bằng một biên bản đếm-0 rồi mới đặt số kiểm kê
    # THẤP HƠN, thay vì đoán một con số tuyệt đối rồi phụ thuộc thứ tự chạy.
    probe = _post(
        client,
        printer_headers,
        "/api/v1/cash-book/count-sheets",
        {
            "branch_id": context.branch_id,
            "cash_account_id": accounts["111"],
            "count_date": "2026-03-31",
            "counted_total": "0",
        },
    )
    book = Decimal(probe["book_balance"])
    assert book >= Decimal(500_000)
    counted = book - Decimal(20_000)

    sheet = _post(
        client,
        printer_headers,
        "/api/v1/cash-book/count-sheets",
        {
            "branch_id": context.branch_id,
            "cash_account_id": accounts["111"],
            "count_date": "2026-03-31",
            "counted_total": str(counted),
            "lines": [{"denomination": str(counted), "quantity": 1}],
        },
    )

    response = client.post(
        f"/api/v1/cash-book/count-sheets/{sheet['id']}/print", headers=printer_headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == PDF_MEDIA_TYPE

    text = _pdf_text(response.content)
    assert "Mẫu số 08a - TT" in text
    assert "Chênh lệch (III = I - II)" in text
    assert f"Loại {format_money(counted, blank_zero=False)}" in text
    assert format_money(book, blank_zero=False) in text
    # Két THIẾU 20.000 so với sổ → dòng III dương (I − II) và lý do hỏi "thiếu".
    assert "20.000" in text
    assert "-20.000" not in text
    assert "Lý do thiếu" in text
    assert "Lý do thừa" not in text


def test_count_sheet_print_needs_the_count_sheet_permission(
    client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Biên bản không có mã quyền `.print` riêng (không phải chứng từ): ai đọc
    được biên bản thì in được. Người chỉ có quyền phiếu thu thì không."""
    sheet = _post(
        client,
        printer_headers,
        "/api/v1/cash-book/count-sheets",
        {
            "branch_id": context.branch_id,
            "cash_account_id": accounts["111"],
            "count_date": "2026-04-30",
            "counted_total": "0",
        },
    )
    receipt_only = ensure_role(
        session_factory,
        dataset_alpha,
        "chi_phieu_thu",
        [permission_code("cash_book", "receipt", action) for action in (Action.VIEW, Action.PRINT)],
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        receipt_only,
        "receipt_only_user",
        test_password,
        branch_codes=[context.branch_code],
    )
    refused = client.post(f"/api/v1/cash-book/count-sheets/{sheet['id']}/print", headers=headers)
    assert refused.status_code == 403


def test_count_sheet_template_stays_out_of_the_voucher_template_list(
    client: TestClient, printer_headers: dict[str, str]
) -> None:
    """`KKQ` không phải loại chứng từ: hộp chọn mẫu của nút In chỉ liệt kê mẫu
    của các loại chứng từ người gọi in được, nên biên bản không lọt vào."""
    response = client.get("/api/v1/print-templates", headers=printer_headers)
    assert response.status_code == 200, response.text
    types = {row["document_type"] for row in response.json()["templates"]}
    assert {"PT", "PC", "UNC", "BC"} <= types
    assert "KKQ" not in types
