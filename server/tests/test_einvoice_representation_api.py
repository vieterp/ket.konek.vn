"""Bản thể hiện hóa đơn + dấu "đã gửi" qua HTTP (lát 7E-3).

Tệp riêng chứ không bồi vào `test_einvoice_api.py` vì hai lý do vận hành: đường
này cần **thư mục kho tệp** cấu hình sẵn (`test_settings` mặc định không có), và
nó cần một ký hiệu hóa đơn của riêng mình — dãy số là trạng thái toàn dataset,
hai tệp dùng chung một ký hiệu sẽ thấy số bắt đầu ở chỗ tệp kia dừng lại (bẫy
thứ tự tệp đã bắt được mười lần từ 7A).

Bốn thứ chỉ đo được ở đây:

* **Lượt đầu dựng và cất, lượt sau đọc từ đĩa.** Đó là toàn bộ lý do
  `representation.py` tồn tại — nghĩa vụ lưu hóa đơn dài hơn hợp đồng với nhà
  cung cấp. Đo bằng cách đếm số lần hàm dựng chạy, không bằng cách so nội dung:
  hai lượt trả cùng nội dung vẫn xanh nếu lượt sau âm thầm dựng lại.
* **Ba câu trả lời khác nhau ra ba mã HTTP khác nhau** (200 / 404 / 503) — nếu
  chúng gộp thành một thì người dùng không biết nên thử lại hay thôi.
* **Hóa đơn chưa phát hành không có bản thể hiện**, chặn trước khi hỏi adapter.
* **Quyền `.print`** canh cửa tải, tách khỏi `.view`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, ensure_role
from conftest import api_test_client
from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.config.printing.voucher_fields import RATE_DECIMALS
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.formatting import format_money, format_quantity
from ket.kernel.persistence.session import control_session
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.kernel.security import account_service, role_service, totp
from ket.kernel.security.keystore import SecretBox
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.modules.einvoice import (
    EINVOICE_PERMISSION_MODULE,
    INVOICE_PERMISSION_CODE,
    REGISTRATION_PERMISSION_CODE,
)
from ket.modules.einvoice.models import (
    EINVOICE_TABLE_NAME,
    EInvoiceRepresentation,
    EInvoiceStatus,
    ErrorNoticeKind,
)
from ket.modules.einvoice.print_details import build_representation_details
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAY_08 = "2026-05-08"

# Khối id 97xx là của các tệp hóa đơn điện tử; 974x thuộc riêng tệp này.
CUSTOMER_ID = 9741
SALESPERSON_ID = 9742

FORM_ID = 8851
SERIAL = "C26TREP"
"""Ký hiệu **và id** riêng của tệp này. Bản đầu lấy 8841 — id mà
`test_einvoice_outbox` đã dùng — và nó xanh khi chạy một mình rồi đỏ trong lượt
chạy đầy đủ: mỗi tệp dựng một **chi nhánh mới**, nên tệp chạy sau đăng ký cùng
một ký hiệu cho chi nhánh khác và đâm vào luật "một ký hiệu thuộc đúng một chi
nhánh" (`invoice_form_branch_owner`, 0032). Ký hiệu riêng thôi là chưa đủ; id
cũng phải riêng, vì `ensure_invoice_form` tra theo id."""

PRINTER_ROLE = "ke_toan_hoa_don_ban_the_hien"
VIEWER_ROLE = "ke_toan_hoa_don_chi_xem"

DATASET_HEADER = "X-Dataset"


@pytest.fixture(scope="module")
def attachments_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("einvoice-representations")


@pytest.fixture(scope="module")
def representation_settings(test_settings: Settings, attachments_dir: Path) -> Settings:
    return test_settings.model_copy(update={"attachments_dir": attachments_dir})


@pytest.fixture(scope="module")
def app_client(
    representation_settings: Settings,
    app_engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(representation_settings)) as client:
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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7E3")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7E3")
        # Không khai `provider_code`: ký hiệu này phát hành **nội bộ**, nên bản
        # thể hiện dựng tại chỗ và bài test không phải giả lập máy chủ nào.
        form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
        ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    return account_ids


def _login(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    *,
    role_code: str,
    prefix: str,
    branch_code: str,
) -> dict[str, str]:
    """Người dùng có vai trò đòi 2FA, đã đăng ký TOTP, đã đăng nhập.

    Cùng khuôn `test_einvoice_api._login_with_totp`: cả hai mã quyền của phân hệ
    khai `requires_second_factor`, nên đăng nhập bằng mật khẩu trần chỉ nhận
    được một phiên hạn chế.
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


def _permissions(*actions: Action) -> list[str]:
    return (
        [
            permission_code(EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, action)
            for action in actions
        ]
        + [
            permission_code(EINVOICE_PERMISSION_MODULE, REGISTRATION_PERMISSION_CODE, action)
            for action in (Action.VIEW, Action.CREATE, Action.EDIT)
        ]
        # Quyền đính kèm chung: **cố tình** cấp cho vai trò của tệp này, vì bài
        # `test_an_arbitrary_attachment_cannot_squat…` phải đính được một tệp
        # giả thì mới đo được điều nó khai. Bản đầu của bài ấy không có quyền
        # này nên lượt đính luôn 403, nhánh 201 không tồn tại, và nó chỉ đo lại
        # "tải bản thể hiện trả 200" — tức kịch bản CRITICAL không ai canh.
        + [
            permission_code(SYSTEM_MODULE, "attachment", action)
            for action in (Action.VIEW, Action.CREATE)
        ]
    )


@pytest.fixture(scope="module")
def printer_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Trọn quyền hóa đơn, **gồm** `.print` — người tải được bản thể hiện."""
    ensure_role(
        session_factory,
        dataset_alpha,
        PRINTER_ROLE,
        _permissions(Action.VIEW, Action.CREATE, Action.EDIT, Action.PRINT),
    )
    return _login(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        secret_box,
        test_password,
        role_code=PRINTER_ROLE,
        prefix="banthehien",
        branch_code=context.branch_code,
    )


@pytest.fixture(scope="module")
def viewer_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Đọc được hóa đơn, **không** `.print`.

    Vai trò này tồn tại để chứng minh cửa tải bản thể hiện canh đúng mã quyền
    của nó. Nếu ai đó đổi cổng sang `.view` thì `Action.PRINT` của 7D thành một
    mã không ai canh, và bài này là thứ đỏ lên.
    """
    ensure_role(
        session_factory,
        dataset_alpha,
        VIEWER_ROLE,
        _permissions(Action.VIEW, Action.CREATE, Action.EDIT),
    )
    return _login(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        secret_box,
        test_password,
        role_code=VIEWER_ROLE,
        prefix="chixem",
        branch_code=context.branch_code,
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
        description="bán hàng cho bài bản thể hiện",
        lines=(
            SalesInvoiceLineIn(
                description="Dịch vụ tư vấn",
                quantity=Decimal(2),
                unit_price_fc=Decimal(1_500_000),
                amount_fc=Decimal(3_000_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(300_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        return str(SalesInvoiceService(session).create(payload, user_id=ACTOR_ID).id)


def _draft_invoice(client: TestClient, headers: dict[str, str], voucher_id: str) -> str:
    created = client.post(
        "/api/v1/einvoices",
        json={"source_voucher_id": voucher_id, "invoice_form_id": FORM_ID},
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _issued_invoice(client: TestClient, headers: dict[str, str], voucher_id: str) -> str:
    """Một tờ hóa đơn đã phát hành nội bộ và đã xác nhận."""
    invoice_id = _draft_invoice(client, headers, voucher_id)
    issued = client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/issue",
        json={"invoice_date": MAY_08},
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    assert issued.status_code == 200, issued.text
    confirmed = client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/confirm",
        json={"tax_authority_code": "M1-7E3", "lookup_code": "TRA-CUU-7E3"},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return invoice_id


def _representations(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    invoice_id: str,
    context: PostingContext,
) -> list[EInvoiceRepresentation]:
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        return list(
            session.scalars(
                select(EInvoiceRepresentation).where(
                    EInvoiceRepresentation.einvoice_id == invoice_id
                )
            )
        )


def test_the_representation_is_rendered_once_and_then_read_from_disk(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lượt đầu dựng và cất; lượt sau **không dựng lại**.

    Đếm số lần hàm dựng chạy chứ không so nội dung hai lượt: hai bản PDF giống
    nhau vẫn xanh nếu lượt sau âm thầm dựng lại, mà chính lượt "đọc lại từ kho"
    mới là thứ giữ cho tờ hóa đơn còn mở được sau ngày hết hợp đồng nhà cung cấp.
    """
    import ket.api.routers.einvoice as router_module

    renders = 0
    original = router_module._render_representation

    def counting(*args: object, **kwargs: object) -> bytes:
        nonlocal renders
        renders += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(router_module, "_render_representation", counting)

    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    first = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert first.status_code == 200, first.text
    assert first.headers["content-type"].startswith("application/pdf")
    assert first.content.startswith(b"%PDF")
    assert "attachment; filename=" in first.headers["content-disposition"]

    second = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert second.status_code == 200, second.text
    assert second.content == first.content
    assert renders == 1, "lượt thứ hai phải đọc từ kho, không dựng lại"

    stored = _representations(session_factory, dataset_alpha, invoice_id, context)
    assert [row.kind for row in stored] == ["pdf"]
    assert stored[0].branch_id == context.branch_id, "tệp thuộc chi nhánh của hóa đơn"


def test_an_internally_issued_invoice_has_no_xml_and_says_so_as_404(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Hóa đơn đặt in / tự in **không có** tệp XML — sự thật pháp lý, không lỗ hổng.

    404 chứ không 503: không lượt thử lại nào làm tệp ấy xuất hiện, và bảo người
    dùng "thử lại sau" là dạy họ bấm mãi vào một thứ không tồn tại.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation",
        params={"kind": "xml"},
        headers=printer_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "einvoice.representation_unavailable"
    assert not _representations(session_factory, dataset_alpha, invoice_id, context)


def test_a_draft_invoice_has_no_representation(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Chưa phát hành thì chưa có số, chưa có ngày — một tờ giấy in ra từ bản nháp
    trông y hệt hóa đơn thật, nên cổng đứng **trước** lượt hỏi adapter."""
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _draft_invoice(app_client, printer_headers, voucher_id)

    response = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert response.status_code == 404, response.text
    assert not _representations(session_factory, dataset_alpha, invoice_id, context)


def test_downloading_the_representation_needs_the_print_permission(
    app_client: TestClient,
    printer_headers: dict[str, str],
    viewer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """`.print`, không `.view`.

    7D khai `Action.PRINT` với đúng nghĩa "xem trước và tải bản thể hiện"
    (FR-EIV-016/026); canh cửa này bằng `.view` sẽ biến mã ấy thành mã không ai
    dùng, và người chỉ được đọc danh sách sẽ tải được tờ hóa đơn.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    refused = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=viewer_headers
    )
    assert refused.status_code == 403, refused.text
    # Cùng người ấy vẫn đọc được hóa đơn — bài này đo đúng một mã quyền.
    listed = app_client.get("/api/v1/einvoices", headers=viewer_headers)
    assert listed.status_code == 200


def test_marking_sent_records_the_claim_and_flips_the_status(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """`DA_PHAT_HANH → DA_GUI`, kèm người nhận và mốc thời gian.

    **Không đòi phải tải bản thể hiện trước.** Ca thường gặp nhất của EasyInvoice
    là chính họ gửi mail theo cấu hình bên họ, nên đòi một tệp cục bộ sẽ chặn
    đúng một lời khai trung thực. Bài này chạy trên một tờ hóa đơn chưa ai tải
    tệp nào, và đó là chủ ý.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/mark-sent",
        json={"sent_to": "ketoan@khachhang.vn; giamdoc@khachhang.vn"},
        headers=printer_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == int(EInvoiceStatus.DA_GUI)
    assert body["sent_to"] == "ketoan@khachhang.vn; giamdoc@khachhang.vn"
    assert body["sent_at"] is not None
    assert not _representations(session_factory, dataset_alpha, invoice_id, context)


def test_a_sent_claim_without_a_recipient_is_refused_at_the_door(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Người nhận rỗng bị chặn ở schema (422) — một dấu "đã gửi" không nói được
    gửi cho ai thì không ai đối chiếu lại được, và hóa đơn phải đứng nguyên."""
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/mark-sent",
        json={"sent_to": ""},
        headers=printer_headers,
    )
    assert response.status_code == 422, response.text

    current = app_client.get("/api/v1/einvoices", headers=printer_headers)
    rows = {row["id"]: row for row in current.json()["items"]}
    assert rows[invoice_id]["status"] == int(EInvoiceStatus.DA_PHAT_HANH)
    assert rows[invoice_id]["sent_at"] is None


def test_the_send_stamp_is_pinned_between_issue_time_and_now(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Ba biên, đo chính xác trên mốc `issued_at` của tờ hóa đơn.

    Lượt gửi xảy ra ngoài phần mềm nên mốc thời gian là thứ người dùng khai, và
    nó nhận ngày lùi — đó là lý do trường ấy tồn tại. Nhưng khoảng hợp lệ có
    thật hai đầu: trên là đồng hồ hiện tại (cùng luật trần `book_date` của
    6F-2, một dòng sổ không đi trước đồng hồ), dưới là mốc cấp số (không gửi
    được thứ chưa tồn tại).

    Bài dựng giá trị từ chính `issued_at` chứ không từ "một giây trước": một tờ
    hóa đơn vừa phát hành có cửa sổ lùi gần bằng không, nên mọi hằng số thời
    gian gõ tay ở đây đều đỏ vì lý do sai.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    listed = app_client.get("/api/v1/einvoices", headers=printer_headers)
    issued_at = datetime.fromisoformat(
        {row["id"]: row for row in listed.json()["items"]}[invoice_id]["issued_at"]
    )

    def mark(stamp: datetime) -> httpx.Response:
        return app_client.post(
            f"/api/v1/einvoices/{invoice_id}/actions/mark-sent",
            json={"sent_to": "ketoan@khachhang.vn", "sent_at": stamp.isoformat()},
            headers=printer_headers,
        )

    future = mark(datetime.now(UTC) + timedelta(days=1))
    assert future.status_code == 422, future.text
    assert future.json()["error_code"] == "einvoice.not_sent"

    before_issue = mark(issued_at - timedelta(seconds=1))
    assert before_issue.status_code == 422, before_issue.text
    assert before_issue.json()["error_code"] == "einvoice.not_sent"

    # Đúng mốc cấp số: biên dưới là **bao gồm**, và đây là mốc lùi xa nhất mà
    # một tờ hóa đơn vừa phát hành còn nhận được.
    accepted = mark(issued_at)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == int(EInvoiceStatus.DA_GUI)


def test_a_transport_failure_is_503_not_500_and_never_404(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nhánh 503 — lý do tồn tại của `RepresentationAvailability.UNKNOWN`.

    Không có bài này thì cả nhánh ấy không đường nào chạm tới trong bộ test, và
    một bản viết lại gộp `UNKNOWN` vào 404 sẽ đi qua toàn bộ cổng. Bản đầu của
    lát này còn tệ hơn: nó **không bắt** ngoại lệ truyền dẫn nào, nên ca phổ
    biến nhất (hết giờ, đứt nối) ra `500 "lỗi không mong muốn"`.

    Đo trên đúng ca ấy: adapter ném như thư viện HTTP ném thật.
    """
    from ket.modules.einvoice.providers.internal import InternalProvider

    def exploding(_self: object, **_kwargs: object) -> object:
        raise TimeoutError("mạng rớt giữa chừng")

    monkeypatch.setattr(InternalProvider, "fetch_representation", exploding)

    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "einvoice.representation_unreachable"
    # Không cất gì cả: một lượt hỏng không được để lại bản ghi trỏ vào hư không.
    assert not _representations(session_factory, dataset_alpha, invoice_id, context)


def test_the_pdf_already_stored_does_not_answer_a_request_for_the_xml(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Hai loại tệp là hai bản ghi, phân biệt bằng cột `kind`.

    Bản đầu phân biệt bằng `media_type`; bỏ phép lọc ấy đi thì toàn bộ bộ test
    vẫn xanh, tức không bài nào canh. Bài này giết đúng phép đột biến đó: đã có
    PDF trong kho rồi mà xin XML thì vẫn phải nhận câu trả lời của XML.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    pdf = app_client.get(f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers)
    assert pdf.status_code == 200, pdf.text

    xml = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation",
        params={"kind": "xml"},
        headers=printer_headers,
    )
    assert xml.status_code == 404, xml.text
    assert [
        row.kind for row in _representations(session_factory, dataset_alpha, invoice_id, context)
    ] == ["pdf"]


def test_an_arbitrary_attachment_cannot_squat_the_representation_slot(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Tệp đính kèm tùy ý gắn vào id hóa đơn **không** chiếm chỗ bản thể hiện.

    Bản đầu của lát này nhận dạng bản thể hiện trong bảng `attachments` dùng
    chung bằng `(entity_type='einvoices', media_type)`. Cửa
    `POST /api/v1/attachments` cho đính bất cứ tệp nào vào bất cứ `entity_id`
    nào, nên một PDF tùy ý chiếm chỗ vĩnh viễn và bản thật không bao giờ được
    lấy về — nghĩa vụ lưu trữ mười năm bị vô hiệu bằng một lượt gọi HTTP.

    Bảng riêng là bản sửa; bài này là thứ giữ cho nó không bị gộp lại.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    planted = app_client.post(
        "/api/v1/attachments",
        data={"entity_type": EINVOICE_TABLE_NAME, "entity_id": invoice_id},
        files={"file": ("gia-mao.pdf", b"KHONG PHAI HOA DON", "application/pdf")},
        headers={**printer_headers, IDEMPOTENCY_HEADER: str(uuid4())},
    )
    # Cửa đính kèm có thể từ chối vì thiếu quyền — cả hai kết cục đều chấp nhận
    # được; điều bài này đo là lượt tải bản thể hiện KHÔNG đọc ra tệp ấy.
    # Lượt đính PHẢI thành công — nếu nó 403 thì bài này không đo gì cả.
    assert planted.status_code == 201, planted.text

    representation = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert representation.status_code == 200, representation.text
    assert representation.content.startswith(b"%PDF")
    assert b"KHONG PHAI HOA DON" not in representation.content


def test_a_hostile_provider_file_name_never_breaks_the_download(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tên tệp do nhà cung cấp đặt đi qua hai lớp lọc, và cả hai đều cần.

    Bản đầu của lát này đưa thẳng `FileName` vào `Content-Disposition`. Starlette
    mã hóa header bằng latin-1, nên một tên **tiếng Việt** — ca thường gặp của
    EasyInvoice — làm cả lượt tải đổ `500`; và vì tệp đã cất xong trước khi dựng
    phản hồi, nó đổ **mãi mãi** cho tờ hóa đơn ấy. Bài này đi qua đúng đường
    `AVAILABLE` (đường nội bộ không bao giờ chạm tên của nhà cung cấp, nên nó
    không đo được gì ở đây) với một tên mang đủ ba loại ký tự nguy hiểm: dấu
    tiếng Việt, xuống dòng, và dấu ngoặc kép.
    """
    from ket.modules.einvoice.providers.contracts import (
        RepresentationAvailability,
        RepresentationOutcome,
    )
    from ket.modules.einvoice.providers.internal import InternalProvider

    hostile = 'Hóa đơn "00000123".pdf\r\nX-Injected: yes'

    def hosted(_self: object, **_kwargs: object) -> RepresentationOutcome:
        return RepresentationOutcome(
            availability=RepresentationAvailability.AVAILABLE,
            content=b"%PDF-1.7 ban the hien cua nha cung cap",
            media_type="application/pdf",
            file_name=hostile,
        )

    monkeypatch.setattr(InternalProvider, "fetch_representation", hosted)

    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    # Không ký tự điều khiển nào lọt ra header, và không tham số nào bị chèn.
    assert "\r" not in disposition and "\n" not in disposition
    assert "x-injected" not in {name.lower() for name in response.headers}
    # Tên đầy đủ vẫn tới nơi qua `filename*` — lọc không được làm mất thông tin.
    assert "filename*=UTF-8''" in disposition

    stored = _representations(session_factory, dataset_alpha, invoice_id, context)
    assert len(stored) == 1
    assert "\r" not in stored[0].file_name and '"' not in stored[0].file_name


def test_a_send_stamp_without_a_timezone_is_refused_at_the_schema(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Mốc gửi thiếu múi giờ ⇒ 422, không phải 500.

    `sent_at` là trường datetime **do client gửi lên đầu tiên của cả dự án**
    (mọi `datetime` khác trong `schemas` là dữ liệu đi ra), nên không quy ước
    sẵn nào đỡ. Một giá trị naive đi tới `mark_sent` rồi đem so với
    `datetime.now(UTC)` là `TypeError` — tức `500 "lỗi không mong muốn"` cho một
    đầu vào người dùng gõ sai. `AwareDatetime` là thứ chặn nó, và không có bài
    này thì đảo nó về `datetime` trần cũng không làm cổng nào đỏ.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    response = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/mark-sent",
        json={"sent_to": "ketoan@khachhang.vn", "sent_at": "2026-05-08T10:00:00"},
        headers=printer_headers,
    )
    assert response.status_code == 422, response.text

    listed = app_client.get("/api/v1/einvoices", headers=printer_headers)
    row = {item["id"]: item for item in listed.json()["items"]}[invoice_id]
    assert row["status"] == int(EInvoiceStatus.DA_PHAT_HANH)
    assert row["sent_at"] is None


def test_cancelling_drops_the_cached_representation_so_the_reprint_carries_the_mark(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Tải trước → hủy → tải lại: tờ giấy phải mang dấu "ĐÃ HỦY".

    Đây là ca **thường gặp nhất**, và là ca mà bản sửa đầu tiên của M-3 bỏ sót:
    dấu trạng thái chỉ đóng được lúc **dựng**, mà `ensure` trả bản đã cất trước
    mọi phép kiểm. Thứ tự thật của kế toán lại đúng là *tải → gửi cho người mua
    → sau đó mới hủy*, tức đúng thứ tự khiến dấu ấy không bao giờ xuất hiện.
    Vòng review pre-landing đo được bằng byte y hệt nhau.
    """
    voucher_id = _new_voucher_id(session_factory, dataset_alpha, context, accounts)
    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)

    before = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert before.status_code == 200, before.text
    assert len(_representations(session_factory, dataset_alpha, invoice_id, context)) == 1

    for kind, number in (
        (ErrorNoticeKind.THONG_BAO_HUY, "TBH-7E3-REP"),
        (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-7E3-REP"),
    ):
        notice = app_client.post(
            f"/api/v1/einvoices/{invoice_id}/notices",
            json={"kind": int(kind), "notice_no": number, "notice_date": MAY_08},
            headers=printer_headers,
        )
        assert notice.status_code == 201, notice.text
        submitted = app_client.post(
            f"/api/v1/einvoices/notices/{notice.json()['id']}/actions/submit",
            headers=printer_headers,
        )
        assert submitted.status_code == 200, submitted.text

    cancelled = app_client.post(
        f"/api/v1/einvoices/{invoice_id}/actions/cancel", headers=printer_headers
    )
    assert cancelled.status_code == 200, cancelled.text
    # Dòng trỏ tới bản cũ đã bỏ; **tệp** thì vẫn nằm trong kho content-hash.
    assert not _representations(session_factory, dataset_alpha, invoice_id, context)

    after = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert after.status_code == 200, after.text
    assert after.content != before.content, "tờ in lại không được giống hệt tờ trước khi hủy"


def test_a_foreign_currency_invoice_prints_its_rate_and_a_rounded_conversion(
    app_client: TestClient,
    printer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Khối tỷ giá / quy đổi của NĐ123 §10 khoản 13, chạy thật một lần.

    Trước bài này, cả nhánh ngoại tệ là **mã chết trong CI**: bài đầu-cuối duy
    nhất chạy trên một hóa đơn VND và chỉ khẳng định tệp trả về bắt đầu bằng
    `%PDF`. Đó là lý do lỗi `total_fc * exchange_rate` (không làm tròn, ra số
    VND tám chữ số thập phân) đi lọt qua cả vòng review thứ nhất.

    Bài không đọc ngược nội dung PDF — nó chỉ chứng minh **đường ấy chạy được**
    đầu-cuối trên một chứng từ ngoại tệ thật; phép tính có bài đơn vị riêng
    (`test_einvoice_print_details`) và có `convert_currency` của kernel canh.
    """
    payload = SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=MAY_08,
        posting_date=MAY_08,
        currency_code="USD",
        # Tỷ giá lẻ có chủ đích: nhân rồi không làm tròn thì con số quy đổi mọc
        # thêm chữ số thập phân, đúng hình dạng lỗi mà bài này canh.
        exchange_rate=Decimal("23456.789012"),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng ngoại tệ cho bài bản thể hiện",
        lines=(
            SalesInvoiceLineIn(
                description="Dịch vụ tư vấn quốc tế",
                quantity=Decimal(3),
                unit_price_fc=Decimal("411.52"),
                amount_fc=Decimal("1234.56"),
                vat_rate=Decimal("8.25"),
                vat_amount_fc=Decimal("101.85"),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        voucher_id = str(SalesInvoiceService(session).create(payload, user_id=ACTOR_ID).id)

    invoice_id = _issued_invoice(app_client, printer_headers, voucher_id)
    response = app_client.get(
        f"/api/v1/einvoices/{invoice_id}/representation", headers=printer_headers
    )
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    assert len(_representations(session_factory, dataset_alpha, invoice_id, context)) == 1

    # Đọc thẳng ô trên bản in — `%PDF` ở trên chỉ chứng minh đường chạy được,
    # không chứng minh con số đúng. Đây là chỗ lỗi thật sự sống.
    with unit_of_work(session_factory, scope) as session:
        details = build_representation_details(
            session, EInvoiceService(session).require(UUID(invoice_id)), user_id=ACTOR_ID
        )
    notes = {field.label: field.value for field in details.notes}
    rate_label = next(label for label in notes if label.startswith("Tỷ giá"))
    converted_label = next(label for label in notes if label.startswith("Tổng tiền thanh toán quy"))

    assert notes[rate_label] == format_quantity(Decimal("23456.789012"), decimals=RATE_DECIMALS)
    # **Nhiều nhất hai chữ số thập phân.** Khẳng định theo HÌNH DẠNG chứ không
    # dựng lại công thức: chép lại `convert_currency` vào đây thì bài trở thành
    # hằng đúng, còn "một số tiền VND không có tám chữ số lẻ" là thứ độc lập với
    # cách tính và giết đúng phép đột biến `total_fc * rate`.
    decimals = notes[converted_label].partition(",")[2]
    assert len(decimals) <= 2, notes[converted_label]
    assert notes[converted_label] != format_money(
        Decimal("1336.41") * Decimal("23456.789012"), blank_zero=False
    )

    # Thuế suất lẻ 8,25% phải in nguyên, không làm tròn thành 8%.
    assert any("8,25%" in cell for row in details.tables[0].rows for cell in row)
