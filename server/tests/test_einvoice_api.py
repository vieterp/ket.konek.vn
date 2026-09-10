"""Phân hệ hóa đơn điện tử qua HTTP (lát 7D, đóng review H-4).

`test_einvoice_flow.py` đi thẳng qua `session` nên nó không chạm bốn thứ chỉ
tồn tại ở tầng API, và cả bốn đều là chỗ một lát sau dễ làm hỏng trong im lặng:

* **Quyền** — FR-EIV-007 đòi quyền hóa đơn điện tử **tách khỏi** quyền kế toán.
  Người có trọn quyền chứng từ bán mà không có `einvoice.invoice.*` phải bị 403.
* **Lớp xác thực thứ hai** — cả hai mã quyền của phân hệ khai
  `requires_second_factor`, nên vai trò cấp chúng bật `totp_required` cho người
  giữ. Bài này đăng nhập bằng TOTP thật; nếu một lát sau ai đó gỡ cờ ấy đi thì
  bước đăng ký TOTP ở đây thành thừa nhưng vẫn chạy — còn nếu cờ được giữ mà
  đường đăng nhập hỏng, bài đỏ ngay.
* **Phạm vi chi nhánh** — hồ sơ đăng ký nhận `branch_id` từ thân request, tức
  người gọi tự khai; `_require_branch_in_scope` là thứ duy nhất chặn họ khai một
  chi nhánh không phải của mình.
* **Idempotency** — lượt gửi lại `POST /einvoices` phải trả `200` kèm **đúng
  hóa đơn cũ**, không tạo tờ thứ hai.

Ký hiệu và khối id của tệp này tách hẳn ba tệp kia (`einvoice_support` giải
thích vì sao mỗi tệp phải có ký hiệu riêng).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, ensure_role
from conftest import api_test_client
from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.api.routers.einvoice import router as einvoice_router
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.session import control_session
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.kernel.security import account_service, role_service, totp
from ket.kernel.security.keystore import SecretBox
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice import (
    EINVOICE_PERMISSION_MODULE,
    INVOICE_PERMISSION_CODE,
    REGISTRATION_PERMISSION_CODE,
)
from ket.modules.einvoice.models import EInvoiceStatus, ErrorNoticeKind
from ket.modules.sales import INVOICE_PERMISSION_CODE as SALES_INVOICE_CODE
from ket.modules.sales import SALES_PERMISSION_MODULE
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAY_08 = "2026-05-08"

CUSTOMER_ID = 9731
SALESPERSON_ID = 9732
FORM_ID = 8831
SERIAL = "C26TAPI"

EINVOICE_ROLE = "ke_toan_hoa_don_dien_tu"
SALES_ONLY_ROLE = "ke_toan_ban_hang_khong_hoa_don"

DATASET_HEADER = "X-Dataset"


def _einvoice_permissions() -> list[str]:
    invoice = [
        permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, action)
        for action in (
            Action.VIEW,
            Action.CREATE,
            Action.EDIT,
            Action.DELETE,
            Action.PRINT,
            Action.EXPORT,
        )
    ]
    registration = [
        permission_code(EINVOICE_PERMISSION_MODULE, REGISTRATION_PERMISSION_CODE, action)
        for action in (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE)
    ]
    return invoice + registration


def _sales_permissions() -> list[str]:
    return [
        permission_code(SALES_PERMISSION_MODULE, SALES_INVOICE_CODE, action) for action in Action
    ]


@pytest.fixture(scope="module")
def app_client(test_settings: Settings) -> Iterator[TestClient]:
    with api_test_client(create_app(test_settings)) as client:
        yield client


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    account_ids = seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7D-API")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7D-API")
        form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
        ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    return account_ids


def _login_with_totp(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    *,
    role_code: str,
    prefix: str,
    branch_codes: list[str],
) -> dict[str, str]:
    """Người dùng có vai trò đòi 2FA, đã đăng ký TOTP, đã đăng nhập.

    Không dùng được `catalog_api_support.actor`: nó đăng nhập bằng mật khẩu
    trần, mà vai trò của phân hệ này bật `totp_required` nên lượt ấy chỉ nhận
    được một phiên hạn chế (`SessionScope.TOTP_ENROLLMENT`). Đây chính là hệ quả
    mà `deployment-guide` cảnh báo người vận hành: gán quyền hóa đơn điện tử là
    bắt người giữ đăng ký TOTP.
    """
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
        actor_permissions=None,
    )
    for branch_code in branch_codes:
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset.schema_name,
            user_id=user.id,
            branch_code=branch_code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )

    with control_session(session_factory) as session:
        enrolling = account_service.find_user(session, user.username)
        uri = account_service.begin_totp_enrollment(session, user=enrolling, secret_box=secret_box)
    secret = uri.split("secret=")[1].split("&")[0]
    generator = pyotp.TOTP(secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS)
    with control_session(session_factory) as session:
        account_service.confirm_totp_enrollment(
            session,
            user=account_service.find_user(session, user.username),
            code=generator.now(),
            secret_box=secret_box,
        )

    # Chu kỳ KHÁC mã vừa dùng để xác nhận: mã đã dùng bị từ chối dùng lại
    # (`TotpCodeReusedError`), và đó là chống phát lại chứ không phải một lỗi.
    later = datetime.now(UTC) + timedelta(seconds=totp.PERIOD_SECONDS)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": user.username,
            "password": test_password,
            "totp_code": generator.at(later),
        },
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['token']}",
        DATASET_HEADER: dataset.code,
    }


@pytest.fixture(scope="module")
def issuer_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    ensure_role(
        session_factory,
        dataset_alpha,
        EINVOICE_ROLE,
        _einvoice_permissions() + _sales_permissions(),
    )
    return _login_with_totp(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        secret_box,
        test_password,
        role_code=EINVOICE_ROLE,
        prefix="hoadon",
        branch_codes=[context.branch_code],
    )


@pytest.fixture(scope="module")
def sales_only_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Trọn quyền chứng từ bán, **không** một mã hóa đơn điện tử nào.

    Đăng nhập bằng mật khẩu trần được, và đó chính là điều đang nói: vai trò này
    không đòi 2FA vì nó không chạm tới hóa đơn.
    """
    from catalog_api_support import actor

    ensure_role(session_factory, dataset_alpha, SALES_ONLY_ROLE, _sales_permissions())
    return actor(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        SALES_ONLY_ROLE,
        "banhang",
        test_password,
        branch_codes=[context.branch_code],
    )


def _new_voucher_id(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> str:
    payload = SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=MAY_08,
        posting_date=MAY_08,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng cho bài API",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(1),
                unit_price_fc=Decimal(300_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        return str(SalesInvoiceService(session).create(payload, user_id=ACTOR_ID).id)


def _create_invoice(
    client: TestClient, headers: dict[str, str], voucher_id: str, *, key: str | None = None
) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/api/v1/einvoices",
        json={"source_voucher_id": voucher_id, "invoice_form_id": FORM_ID},
        headers={**headers, IDEMPOTENCY_HEADER: key or str(uuid4())},
    )
    return response.status_code, response.json()


def test_sales_permission_alone_does_not_issue_invoices(
    app_client: TestClient,
    sales_only_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """FR-EIV-007 — quyền hóa đơn điện tử tách khỏi quyền kế toán.

    Người này lập được chứng từ bán và ghi sổ được nó; họ **không** được phát
    hành hóa đơn đỏ. Gộp hai quyền là để cả phòng kế toán phát hành hóa đơn có
    hệ quả pháp lý chỉ vì họ nhập được chứng từ.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    status_code, _ = _create_invoice(app_client, sales_only_headers, voucher_id)
    assert status_code == 403

    listed = app_client.get("/api/v1/einvoices", headers=sales_only_headers)
    assert listed.status_code == 403


def test_the_full_lifecycle_over_http(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Lập → phát hành → xác nhận → hai văn bản → nộp → hủy, qua đúng các route thật."""
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    status_code, created = _create_invoice(app_client, issuer_headers, voucher_id)
    assert status_code == 201, created
    invoice_id = created["id"]
    assert created["invoice_no"] is None

    issued = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/issue",
        json={"invoice_date": MAY_08},
        headers={**issuer_headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    assert issued.status_code == 200, issued.text
    assert issued.json()["status"] == int(EInvoiceStatus.DANG_PHAT_HANH)
    assert len(issued.json()["invoice_no"]) == 8

    confirmed = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/confirm",
        json={"tax_authority_code": "M1-26-API", "lookup_code": "TRA-CUU-API"},
        headers=issuer_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == int(EInvoiceStatus.DA_PHAT_HANH)

    # Hủy khi chưa đủ hai văn bản đã nộp: 422, không phải 500.
    refused = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/cancel", headers=issuer_headers
    )
    assert refused.status_code == 422, refused.text

    for kind, number in (
        (ErrorNoticeKind.THONG_BAO_HUY, "TBH-API"),
        (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-API"),
    ):
        notice = app_client.post(
            f"/api/v1/einvoices/{invoice_id}/notices",
            json={"kind": int(kind), "notice_no": number, "notice_date": MAY_08},
            headers=issuer_headers,
        )
        assert notice.status_code == 201, notice.text
        submitted = app_client.post(
            f"/api/v1/einvoices/notices/{notice.json()['id']}/actions/submit",
            headers=issuer_headers,
        )
        assert submitted.status_code == 200, submitted.text

    cancelled = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/cancel", headers=issuer_headers
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == int(EInvoiceStatus.DA_HUY)


def test_the_error_flow_table_is_not_shadowed_by_the_id_route(
    app_client: TestClient,
    issuer_headers: dict[str, str],
) -> None:
    """`GET /error-flows` phải tới được, không bị `GET /{einvoice_id}` nuốt.

    FastAPI khớp route **theo thứ tự khai**, và `{einvoice_id}` là `UUID` — nên
    một lượt khai sau sẽ biến đường này thành `422 uuid_parsing` vĩnh viễn. Bài
    ngắn, nhưng nó canh đúng thứ mà không bài service nào nhìn thấy.
    """
    response = app_client.get("/api/v1/einvoices/error-flows", headers=issuer_headers)
    assert response.status_code == 200, response.text
    flows = response.json()
    # Năm bộ câu trả lời, bốn cách xử lý phân biệt (`docs/srs/07` §4.4).
    assert len(flows) == 5
    assert len({flow["remedy"] for flow in flows}) == 4


def test_an_adjustment_answers_with_409_and_the_right_remedy(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Nhánh chưa thi hành được trả `409`, không `422` và không `500`.

    `422` là "dữ liệu anh gửi sai" — mà bộ câu trả lời ở đây **đúng**, và câu trả
    lời của hệ thống cũng đúng; thứ chưa sẵn sàng là đường thi hành. Nhầm hai mã
    ấy thì client dựng câu "kiểm tra lại số liệu" cho một người không nhập gì sai.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    _, created = _create_invoice(app_client, issuer_headers, voucher_id)
    invoice_id = created["id"]
    app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/issue",
        json={"invoice_date": MAY_08},
        headers={**issuer_headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/confirm",
        json={"tax_authority_code": "M1-26-ADJ", "lookup_code": "TRA-CUU-ADJ"},
        headers=issuer_headers,
    )

    refused = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/resolve-error",
        json={
            "error_kind": 1,
            "buyer_declared": True,
            "notice_no": "04SS-API",
            "notice_date": MAY_08,
        },
        headers=issuer_headers,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "einvoice.remedy_not_available"


def test_resending_the_create_request_returns_the_same_invoice(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """FR-NFR-004 — gửi lại không sinh tờ hóa đơn thứ hai.

    Và từ review H-1, lượt gửi lại **không thể** sinh tờ thứ hai kể cả khi
    idempotency hỏng: chỉ mục riêng phần cho một chứng từ đúng một hóa đơn còn
    hiệu lực. Hai lớp cho một luật; bài này đo lớp trên.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    key = str(uuid4())
    first_code, first = _create_invoice(app_client, issuer_headers, voucher_id, key=key)
    second_code, second = _create_invoice(app_client, issuer_headers, voucher_id, key=key)

    assert first_code == 201
    assert second_code == 200, second
    assert second["id"] == first["id"]


def test_a_second_invoice_for_one_voucher_is_refused(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Khóa mới, cùng chứng từ (review H-1) — phải là lỗi nghiệp vụ, không 500."""
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    first_code, _ = _create_invoice(app_client, issuer_headers, voucher_id)
    second_code, body = _create_invoice(app_client, issuer_headers, voucher_id)

    assert first_code == 201
    assert second_code in {409, 422}, body


def test_a_registration_for_a_branch_outside_the_scope_is_refused(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """`branch_id` do người gọi khai, nên phạm vi phải kiểm ở đúng chỗ nhận nó."""
    other = seed_posting_context(session_factory, dataset_alpha)
    response = app_client.post(
        "/api/v1/einvoices/registrations",
        json={
            "branch_id": other.branch_id,
            "invoice_form_id": FORM_ID,
            "start_date": "2026-01-01",
        },
        headers={**issuer_headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    assert response.status_code == 403, response.text


def test_the_listing_pages_and_counts_the_whole_scope(
    app_client: TestClient,
    issuer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """FR-EIV-015 — một trang, cộng số đếm của TOÀN phạm vi.

    Thẻ lọc trên UI nói "còn bao nhiêu việc"; nếu nó đếm theo trang thì con số
    tụt xuống mỗi lần người dùng lật trang.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    assert _create_invoice(app_client, issuer_headers, voucher_id)[0] == 201

    response = app_client.get(
        "/api/v1/einvoices",
        params={"status": int(EInvoiceStatus.CHUA_PHAT_HANH), "page_size": 1},
        headers=issuer_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) <= 1
    assert body["page_size"] == 1
    assert body["total"] >= 1
    assert body["counts_by_status"][str(int(EInvoiceStatus.CHUA_PHAT_HANH))] == body["total"]


def test_there_is_no_route_that_edits_an_invoice() -> None:
    """BR-EIV-01 ở tầng bề mặt API: **không có** đường sửa nào để mà thử.

    Trigger DB là lớp thứ hai và `test_einvoice_flow` đo nó. Lớp thứ nhất là
    việc endpoint ấy không tồn tại — và một lát sau thêm một `PUT` "cho tiện"
    sẽ làm bài này đỏ trước khi kịp có ai dùng nó.
    """
    paths = {
        str(route.path): set(getattr(route, "methods", set()))  # type: ignore[attr-defined]
        for route in einvoice_router.routes
    }
    assert paths, "không tìm thấy route nào của phân hệ hóa đơn điện tử"
    for path, methods in paths.items():
        assert "PUT" not in methods and "PATCH" not in methods, path


# --- hồ sơ nhà cung cấp và lượt dọn hàng đợi (lát 7E-2) ---------------------


def test_a_provider_profile_round_trip_never_returns_the_password(
    app_client: TestClient, issuer_headers: dict[str, str]
) -> None:
    """Khai hồ sơ đăng nhập rồi đọc lại — mật khẩu **không** ở bất kỳ đâu.

    Bí mật lưu đã mã hóa, nhưng bản mã vẫn là thứ mang đi thử ngoại tuyến được,
    nên nó cũng không được ra khỏi API. Bài này khẳng định trên **toàn bộ thân
    response** chứ không trên một trường cụ thể: thêm một trường mới vô tình chở
    nó ra sẽ đỏ, còn một `assert "password" not in body` thì không.
    """
    secret = "mat-khau-rat-bi-mat-7e2"
    payload = {
        "provider_code": "easyinvoice",
        "base_url": "https://sandbox.easyinvoice.example.vn",
        "username": "nguoi-dung-ei",
        "password": secret,
        "tax_code": "0101234567",
    }

    created = app_client.put(
        "/api/v1/einvoices/provider-profiles", json=payload, headers=issuer_headers
    )
    assert created.status_code == 200, created.text
    assert secret not in created.text

    listed = app_client.get("/api/v1/einvoices/provider-profiles", headers=issuer_headers)
    assert listed.status_code == 200, listed.text
    assert secret not in listed.text
    rows = listed.json()
    mine = [row for row in rows if row["provider_code"] == "easyinvoice"]
    assert len(mine) == 1, "một dòng cho mỗi nhà cung cấp"
    assert mine[0]["username"] == "nguoi-dung-ei"
    assert "password" not in mine[0]


def test_declaring_the_same_provider_twice_updates_instead_of_duplicating(
    app_client: TestClient, issuer_headers: dict[str, str]
) -> None:
    """`PUT` **đặt**, không thêm.

    Hai dòng cho một nhà cung cấp thì `invoice_forms.provider_code` không còn trỏ
    được vào đâu cả — ràng buộc duy nhất ở tầng bảng canh chiều ấy, và bài này
    canh chiều API không bao giờ chạm tới nó.
    """
    base = {
        "provider_code": "easyinvoice",
        "base_url": "https://sandbox.easyinvoice.example.vn",
        "username": "lan-mot",
        "password": "mk-1",
        "tax_code": "0101234567",
    }
    first = app_client.put("/api/v1/einvoices/provider-profiles", json=base, headers=issuer_headers)
    assert first.status_code == 200, first.text

    second = app_client.put(
        "/api/v1/einvoices/provider-profiles",
        json={**base, "username": "lan-hai", "password": "mk-2"},
        headers=issuer_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"], "phải sửa đúng dòng cũ"
    assert second.json()["username"] == "lan-hai"


def test_a_provider_profile_over_plain_http_is_refused(
    app_client: TestClient, issuer_headers: dict[str, str]
) -> None:
    """Địa chỉ không mã hóa bị từ chối.

    Header xác thực của nhà cung cấp mang mật khẩu **dạng rõ** theo đặc tả của
    họ, nên `http://` để lộ nó trên đường truyền.
    """
    refused = app_client.put(
        "/api/v1/einvoices/provider-profiles",
        json={
            "provider_code": "ncc-khong-ma-hoa",
            "base_url": "http://khong-ma-hoa.example.vn",
            "username": "u",
            "password": "p",
            "tax_code": "0101234567",
        },
        headers=issuer_headers,
    )
    assert refused.status_code >= 400, refused.text


def test_declaring_a_provider_profile_needs_the_registration_permission(
    app_client: TestClient, sales_only_headers: dict[str, str]
) -> None:
    """Người chỉ có quyền bán hàng không khai được hồ sơ.

    Khai sai địa chỉ máy chủ là đổi nơi **mọi** tờ hóa đơn của doanh nghiệp được
    gửi tới — cùng mức hệ quả với việc tự cấp cho mình một dải số, nên nó dùng
    quyền hồ sơ đăng ký (có lớp xác thực thứ hai).
    """
    refused = app_client.put(
        "/api/v1/einvoices/provider-profiles",
        json={
            "provider_code": "easyinvoice",
            "base_url": "https://sandbox.easyinvoice.example.vn",
            "username": "u",
            "password": "p",
            "tax_code": "0101234567",
        },
        headers=sales_only_headers,
    )
    assert refused.status_code == 403, refused.text


def test_pumping_the_outbox_queues_a_job(
    app_client: TestClient, issuer_headers: dict[str, str]
) -> None:
    """Nút dọn hàng đợi xếp một lượt bơm và trả về id của nó.

    Đây là đường **duy nhất** đưa một dòng `needs_reconcile` về đích khi chi
    nhánh chưa phát hành thêm hóa đơn nào: loại job ấy khai `direct_enqueue=False`
    nên `POST /api/v1/jobs` không xếp được, và bản cài chưa có bộ lập lịch nào.
    """
    accepted = app_client.post("/api/v1/einvoices/outbox/actions/pump", headers=issuer_headers)

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["job_id"]


def test_reading_the_outbox_needs_only_view_permission(
    app_client: TestClient, issuer_headers: dict[str, str]
) -> None:
    """Panel hàng đợi là cửa **đọc** — và nó không lộ khóa chống trùng.

    `client_ref` là khóa dùng với nhà cung cấp; lộ nó ra API là mời một client
    tự dựng lượt gửi mang đúng khóa ấy, tức đi vòng qua chính cơ chế chống trùng.
    """
    listed = app_client.get("/api/v1/einvoices/outbox", headers=issuer_headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert "due_now" in body
    assert "client_ref" not in listed.text
