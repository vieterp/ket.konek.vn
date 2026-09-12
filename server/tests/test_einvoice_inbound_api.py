"""Hóa đơn điện tử đầu vào qua HTTP (lát 7F-2b, FR-EIV-040).

Tệp riêng chứ không bồi vào `test_einvoice_inbound.py` vì cùng lý do vận hành
đã tách `test_einvoice_representation_api.py`: đường này cần **thư mục kho tệp**
cấu hình sẵn (`test_settings` mặc định không có), và nó cần một ứng dụng dựng
trên bộ cấu hình ấy.

Năm thứ chỉ đo được ở tầng này:

* **Ghép hai phân hệ ở tầng `api`.** Lượt "lập chứng từ mua từ tờ hóa đơn" chạm
  cả `einvoice` lẫn `purchase`, mà luật C3 cấm hai module nhìn nhau. Bài ở đây
  chứng minh phép ghép chạy được **qua cửa HTTP** chứ không chỉ trên giấy.
* **Hai mã quyền, không một.** `einvoice.inbound.create` một mình không được
  dựng chứng từ mua — nếu không thì cửa này là đường vòng lập chứng từ mua mà
  không cần quyền lập chứng từ mua.
* **Phép kiểm tổng** trên một tờ hóa đơn có chiết khấu thương mại: tổng các
  dòng lớn hơn tổng phải trả, và chứng từ dựng thẳng từ dòng sẽ ghi thừa đúng
  phần chiết khấu.
* **Tệp tải về là tệp gốc từng byte** — thứ mang chữ ký số của người bán, và là
  thứ duy nhất có giá trị đối chiếu với cơ quan thuế.
* **Thứ tự đăng ký router.** `GET /api/v1/einvoices/inbound` phải tới đúng
  router của lát này chứ không bị đọc thành một `einvoice_id`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, ensure_role
from conftest import api_test_client
from einvoice_support import inbound_lines_with, inbound_xml, related_invoice_block
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice import EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_vendor, seed_purchase_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
OUR_TAX_CODE = "0312345688"
SELLER_TAX_CODE = "0101243199"
STRANGER_TAX_CODE = "0109999999"

# Khối id 97xx là của các tệp hóa đơn điện tử; 977x thuộc riêng tệp này.
VENDOR_ID = 9771
STRANGER_VENDOR_ID = 9772

DATASET_HEADER = "X-Dataset"
BRANCH_HEADER = "X-Branch"

FULL_ROLE = "ke_toan_hoa_don_dau_vao_day_du"
INBOUND_ONLY_ROLE = "ke_toan_hoa_don_dau_vao_chi_nap"

INBOUND_URL = "/api/v1/einvoices/inbound"


@pytest.fixture(scope="module")
def attachments_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("einvoice-inbound")


@pytest.fixture(scope="module")
def inbound_settings(test_settings: Settings, attachments_dir: Path) -> Settings:
    return test_settings.model_copy(update={"attachments_dir": attachments_dir})


@pytest.fixture(scope="module")
def app_client(
    inbound_settings: Settings,
    app_engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(inbound_settings)) as client:
        yield client


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    account_ids = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        branch = session.get(Branch, context.branch_id)
        assert branch is not None
        branch.tax_code = OUR_TAX_CODE
        ensure_vendor(session, partner_id=VENDOR_ID, code="NCC-7F2B-API", tax_code=SELLER_TAX_CODE)
        ensure_vendor(session, partner_id=STRANGER_VENDOR_ID, code="NCC-7F2B-API-LA")
    return account_ids


def _permissions(*actions: Action) -> list[str]:
    return [
        permission_code(EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE, action)
        for action in actions
    ]


def _headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    *,
    role_code: str,
    prefix: str,
) -> dict[str, str]:
    """Người dùng đã có vai trò, đã gán chi nhánh, đã đăng nhập.

    Đăng nhập bằng mật khẩu trần chứ không qua TOTP: `einvoice.inbound` cố ý
    **không** khai `requires_second_factor`, khác hai mã quyền còn lại của phân
    hệ. Ranh giới là hệ quả pháp lý — phát hành một tờ hóa đơn tạo ra nghĩa vụ
    thuế, nhận một tờ thì không.
    """
    user = user_factory(prefix, password=test_password)
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
        branch_code=context.branch_code,
        actor_user_id=user.id,
        actor_branch_ids=None,
    )
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": test_password}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['token']}",
        DATASET_HEADER: dataset.code,
        # `X-Branch` mang **id**, không phải mã chi nhánh — xem `_acting_branch`.
        BRANCH_HEADER: str(context.branch_id),
    }


@pytest.fixture(scope="module")
def full_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, str]:
    ensure_role(
        session_factory,
        dataset_alpha,
        FULL_ROLE,
        _permissions(Action.VIEW, Action.CREATE, Action.DELETE, Action.PRINT)
        + [
            permission_code("purchase", "invoice", action)
            for action in (Action.VIEW, Action.CREATE)
        ],
    )
    return _headers(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        role_code=FULL_ROLE,
        prefix="hddv-full",
    )


@pytest.fixture(scope="module")
def inbound_only_headers(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, str]:
    """Vai trò **thiếu** quyền lập chứng từ mua — đối trọng của bài quyền kép."""
    ensure_role(
        session_factory,
        dataset_alpha,
        INBOUND_ONLY_ROLE,
        _permissions(Action.VIEW, Action.CREATE),
    )
    return _headers(
        app_client,
        session_factory,
        dataset_alpha,
        user_factory,
        test_password,
        context,
        role_code=INBOUND_ONLY_ROLE,
        prefix="hddv-nap",
    )


_COUNTER = {"next": 0}


def _unique_number() -> str:
    _COUNTER["next"] += 1
    return f"API{_COUNTER['next']:05d}"


def _xml(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "buyer_tax_code": OUR_TAX_CODE,
        "seller_tax_code": SELLER_TAX_CODE,
        "number": _unique_number(),
    }
    payload.update(overrides)
    return inbound_xml(**payload)  # type: ignore[arg-type]


def _upload(
    client: TestClient, headers: dict[str, str], content: bytes, *, name: str = "hoa-don.xml"
) -> tuple[int, dict[str, object]]:
    response = client.post(
        f"{INBOUND_URL}/import",
        headers={**headers, IDEMPOTENCY_HEADER: str(uuid4())},
        files={"file": (name, content, "application/xml")},
    )
    body: dict[str, object] = response.json() if response.content else {}
    return response.status_code, body


def _accounting(accounts: dict[str, int], **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": 1,
        "operation_code": "mua-dich-vu",
        "payable_account_id": accounts["331"],
        "account_id": accounts["642"],
        "vat_account_id": accounts["1331"],
    }
    payload.update(overrides)
    return payload


def test_uploading_a_standard_invoice_stores_it_with_every_line(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    """Lượt nạp trả về tờ hóa đơn kèm dòng, và nó **chưa** lập chứng từ nào.

    `vendor_id` có giá trị vì mã số thuế người bán khớp đúng một dòng danh mục
    — đó là thứ màn hình dùng để phân biệt "sẵn sàng vào sổ" với "chưa khớp
    đối tác".
    """
    status, body = _upload(app_client, full_headers, _xml())

    assert status == 201, body
    assert body["voucher_id"] is None
    assert body["vendor_id"] == VENDOR_ID
    assert len(body["lines"]) == 2  # type: ignore[arg-type]
    assert Decimal(str(body["total_amount"])) == Decimal(2_200_000)


def test_the_same_invoice_uploaded_twice_is_refused_even_as_a_different_file(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    """Khóa nhận dạng chặn ở tầng bảng, không ở khóa idempotency.

    Hai lượt gọi mang **hai khóa idempotency khác nhau** và hai tệp khác nhau
    từng byte, nên thứ chặn lượt sau chỉ có thể là danh tính pháp lý của tờ hóa
    đơn. Đó là bảo đảm bền hơn một khóa idempotency: nó không hết hạn.
    """
    number = _unique_number()
    note = """        <HHDVu>
          <TChat>4</TChat>
          <STT>9</STT>
          <THHDVu>Bản tải từ cổng tra cứu</THHDVu>
        </HHDVu>"""

    first, _ = _upload(app_client, full_headers, _xml(number=number))
    second, body = _upload(
        app_client, full_headers, _xml(number=number, lines=inbound_lines_with(note))
    )

    assert first == 201
    assert second == 409, body


def test_an_invoice_issued_to_another_company_never_reaches_the_book(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    status, body = _upload(app_client, full_headers, _xml(buyer_tax_code=STRANGER_TAX_CODE))

    assert status == 422, body
    assert body["error_code"] == "einvoice.inbound_buyer_mismatch"


def test_an_entity_bomb_is_a_data_error_at_the_door(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    """Tệp thù địch dừng ở cửa với `422`, không thành `500`.

    Quan trọng ở tầng này chứ không chỉ ở bộ phân giải: `500` nghĩa là ngoại lệ
    đã đi qua handler lỗi chung, và một đường tấn công trả về "lỗi không mong
    đợi" là một đường tấn công không ai đọc log ra.
    """
    bomb = '<!DOCTYPE HDon [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;">]>\n'
    status, body = _upload(app_client, full_headers, _xml(prologue=bomb))

    assert status == 422, body
    assert body["error_code"] == "einvoice.inbound_xml_invalid"


def test_creating_the_purchase_voucher_copies_the_invoice_identity(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """Ca dùng trung tâm của lát: tờ hóa đơn thành chứng từ mua.

    Chứng từ mang **danh tính tờ hóa đơn** (mẫu số, ký hiệu, số, ngày) ở đúng
    những cột 7B đã dựng cho nó, và tổng bằng tổng tờ hóa đơn khai. Lượt gọi
    thứ hai `409`: `voucher_id` là một cột, nên "một tờ, một chứng từ" là điều
    chính cấu trúc đã quy định.
    """
    _, invoice = _upload(app_client, full_headers, _xml())
    inbound_id = invoice["id"]

    response = app_client.post(
        f"{INBOUND_URL}/{inbound_id}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert response.status_code == 201, response.text
    linked = response.json()
    assert linked["voucher_id"] is not None

    voucher = app_client.get(
        f"/api/v1/purchase/invoices/{linked['voucher_id']}", headers=full_headers
    )
    assert voucher.status_code == 200, voucher.text
    body = voucher.json()
    assert body["vendor_id"] == VENDOR_ID
    assert body["vendor_invoice_no"] == invoice["invoice_no"]
    assert body["vendor_invoice_serial"] == invoice["invoice_serial"]
    assert body["vendor_invoice_date"] == invoice["invoice_date"]
    # So bằng `Decimal` chứ không bằng chuỗi: hai schema dựng số theo hai cách
    # (`2200000` và `2200000.00`), và điều bài này khẳng định là hai **số tiền**
    # bằng nhau, không phải hai cách viết giống nhau.
    assert Decimal(str(body["total_fc"])) == Decimal(str(invoice["total_amount"]))

    again = app_client.post(
        f"{INBOUND_URL}/{inbound_id}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert again.status_code == 409, again.text


def test_an_unknown_seller_stops_the_voucher_but_not_the_upload(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """Người bán lạ: tờ hóa đơn **vẫn vào sổ**, chứng từ thì chưa.

    Nghĩa vụ lưu trữ không chờ danh mục. Thứ chưa làm được là một khoản phải trả
    không nói được trả cho ai — và câu trả lời cho nó là người dùng chọn đối
    tác, chứ không phải một dòng danh mục mọc ra từ tệp XML.
    """
    status, invoice = _upload(app_client, full_headers, _xml(seller_tax_code="0100000001"))
    assert status == 201
    assert invoice["vendor_id"] is None

    refused = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error_code"] == "einvoice.inbound_vendor_unmatched"

    chosen = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts, vendor_id=STRANGER_VENDOR_ID),
    )
    assert chosen.status_code == 201, chosen.text


def test_an_invoice_whose_lines_do_not_add_up_never_becomes_a_voucher(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """Hóa đơn có chiết khấu thương mại trên tổng — tổng dòng > tổng phải trả.

    Chứng từ dựng thẳng từ dòng sẽ ghi thừa đúng phần chiết khấu, và không phép
    kiểm kế toán nào thấy: chứng từ ấy tự nó cân. Phép kiểm tổng là chỗ duy
    nhất nói ra, và nó từ chối thay vì đoán cách trừ đi phần chênh.
    """
    status, invoice = _upload(
        app_client,
        full_headers,
        _xml(total_before_tax="1800000", total_vat="180000", total_amount="1980000"),
    )
    assert status == 201

    refused = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error_code"] == "einvoice.inbound_totals_mismatch"


def test_uploading_alone_does_not_grant_the_right_to_post_a_purchase(
    app_client: TestClient,
    full_headers: dict[str, str],
    inbound_only_headers: dict[str, str],
    accounts: dict[str, int],
) -> None:
    """Hai mã quyền, không một.

    Chứng từ dựng ra ở đây là chứng từ **mua**, nên nó phải đi qua đúng những mã
    quyền mà một chứng từ mua lập bằng tay phải đi qua. Thiếu bài này thì
    `einvoice.inbound.create` là một đường vòng lập chứng từ mua mà không cần
    quyền lập chứng từ mua.
    """
    status, invoice = _upload(app_client, inbound_only_headers, _xml())
    assert status == 201, invoice

    refused = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**inbound_only_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert refused.status_code == 403, refused.text


def test_the_stored_file_comes_back_byte_for_byte(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    """Tệp gốc, không một bản dựng lại.

    Thứ có giá trị đối chiếu với cơ quan thuế là tệp mang chữ ký số của người
    bán, và mọi lượt sinh lại đều làm mất chữ ký ấy.
    """
    content = _xml()
    _, invoice = _upload(app_client, full_headers, content, name="hoa-don-goc.xml")

    downloaded = app_client.get(f"{INBOUND_URL}/{invoice['id']}/xml", headers=full_headers)

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"


def test_the_pending_list_is_the_question_the_screen_asks(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """ "Còn tờ nào chưa vào sổ" — và lập chứng từ làm con số ấy giảm đúng một.

    Đo bằng **hiệu** chứ không bằng con số tuyệt đối: bảng là trạng thái toàn
    dataset và những bài chạy trước để lại tờ hóa đơn của chúng, nên một khẳng
    định tuyệt đối sẽ đỏ tùy thứ tự chạy.
    """
    before = app_client.get(f"{INBOUND_URL}?pending_only=true", headers=full_headers)
    assert before.status_code == 200, before.text
    start = before.json()["pending"]

    _, invoice = _upload(app_client, full_headers, _xml())
    after_upload = app_client.get(f"{INBOUND_URL}?pending_only=true", headers=full_headers)
    assert after_upload.json()["pending"] == start + 1

    created = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert created.status_code == 201, created.text

    after_voucher = app_client.get(f"{INBOUND_URL}?pending_only=true", headers=full_headers)
    assert after_voucher.json()["pending"] == start


def test_a_pending_invoice_can_be_deleted_and_a_linked_one_cannot(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    _, pending = _upload(app_client, full_headers, _xml())
    removed = app_client.delete(f"{INBOUND_URL}/{pending['id']}", headers=full_headers)
    assert removed.status_code == 204, removed.text
    assert app_client.get(f"{INBOUND_URL}/{pending['id']}", headers=full_headers).status_code == 404

    _, linked = _upload(app_client, full_headers, _xml())
    app_client.post(
        f"{INBOUND_URL}/{linked['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    refused = app_client.delete(f"{INBOUND_URL}/{linked['id']}", headers=full_headers)
    assert refused.status_code == 409, refused.text


def test_the_posting_date_defaults_to_the_invoice_date(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """Bỏ trống ngày hạch toán thì lấy ngày trên tờ hóa đơn — và chọn thì được chọn."""
    _, invoice = _upload(app_client, full_headers, _xml())
    chosen = date(2026, 3, 31).isoformat()

    created = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts, posting_date=chosen),
    )
    assert created.status_code == 201, created.text

    voucher = app_client.get(
        f"/api/v1/purchase/invoices/{created.json()['voucher_id']}", headers=full_headers
    ).json()
    assert voucher["posting_date"] == chosen
    assert voucher["document_date"] == invoice["invoice_date"]


@pytest.mark.parametrize("nature", ["3", "2", None])
def test_an_invoice_about_another_invoice_stores_but_never_becomes_a_voucher(
    app_client: TestClient,
    full_headers: dict[str, str],
    accounts: dict[str, int],
    nature: str | None,
) -> None:
    """Điều chỉnh và thay thế: **vào sổ được, lập chứng từ thì không** (user chốt).

    Tờ hóa đơn vẫn phải lưu — nghĩa vụ lưu trữ không phân biệt loại — và vẫn
    phải nhìn thấy được trong danh sách việc cần làm. Thứ không làm được là
    biến nó thành dữ liệu kế toán: một tờ **điều chỉnh giảm** tự nó nhất quán
    từng đồng nên nó lọt mọi phép kiểm tổng rồi thành một chứng từ mua làm
    **tăng** chi phí và thuế đầu vào đúng phần đáng lẽ phải giảm.

    Phản hồi mang cả danh tính tờ gốc, vì người dùng phải **tra được** nó trong
    sổ để lập chứng từ bằng tay.
    """
    status, invoice = _upload(
        app_client, full_headers, _xml(related=related_invoice_block(nature=nature))
    )

    assert status == 201, invoice
    assert invoice["nature"] != 1
    assert invoice["related_no"] == "00001111"

    refused = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=_accounting(accounts),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error_code"] == "einvoice.inbound_not_original"


def test_a_vat_invoice_without_a_tax_account_says_which_field_is_missing(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """`vat_account_id` tùy chọn theo **dữ liệu**, không tùy chọn vô điều kiện.

    Để `PurchaseInvoiceLineIn` nói hộ thì lỗi là một `ValidationError` của
    pydantic dựng **bên trong** handler — không phải `RequestValidationError`
    của FastAPI, nên không handler nào bắt và nó rơi thẳng vào lưới `500`. Một
    lỗi nhập liệu thường gặp không được trả về "đã xảy ra lỗi không mong muốn".
    """
    _, invoice = _upload(app_client, full_headers, _xml())
    body = _accounting(accounts)
    del body["vat_account_id"]

    refused = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=body,
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error_code"] == "einvoice.inbound_account_missing"


def test_an_invoice_with_no_vat_needs_no_tax_account(
    app_client: TestClient, full_headers: dict[str, str], accounts: dict[str, int]
) -> None:
    """Đối trọng của bài trên — nếu không có nó, "tùy chọn" thành "bắt buộc"
    và bài kia vẫn xanh."""
    exempt_line = """        <HHDVu>
          <STT>1</STT>
          <THHDVu>Dịch vụ không chịu thuế</THHDVu>
          <ThTien>500000</ThTien>
          <TSuat>KCT</TSuat>
        </HHDVu>"""
    _, invoice = _upload(
        app_client,
        full_headers,
        _xml(
            lines=exempt_line,
            vat_groups="",
            total_before_tax="500000",
            total_vat="0",
            total_amount="500000",
        ),
    )
    body = _accounting(accounts)
    del body["vat_account_id"]

    created = app_client.post(
        f"{INBOUND_URL}/{invoice['id']}/actions/create-purchase",
        headers={**full_headers, IDEMPOTENCY_HEADER: str(uuid4())},
        json=body,
    )

    assert created.status_code == 201, created.text


def test_an_oversized_upload_is_called_a_size_problem(
    app_client: TestClient, full_headers: dict[str, str], inbound_settings: Settings
) -> None:
    """Tệp quá khổ đi qua bộ phân giải sẽ đổ với câu "không phải XML đọc được".

    Đúng về hiện tượng (khối byte bị cắt ngang), sai hẳn về nguyên nhân — và
    người dùng đi sửa một tệp không hỏng.
    """
    padding = b"<Bo>" + b"x" * inbound_settings.attachment_max_bytes + b"</Bo>"
    status, body = _upload(app_client, full_headers, _xml() + padding)

    assert status == 413, body
    assert body["error_code"] == "attachment.too_large"


def test_the_list_endpoint_is_not_shadowed_by_the_invoice_route(
    app_client: TestClient, full_headers: dict[str, str]
) -> None:
    """Thứ tự đăng ký router là thứ chịu tải, và một dòng chú thích không canh nó.

    Router hóa đơn có `GET /api/v1/einvoices/{einvoice_id}`, nên đăng ký nó
    trước sẽ làm `inbound` bị đọc thành một `einvoice_id` và lượt gọi này trả
    `422` ở phép ép UUID thay vì một trang dữ liệu.
    """
    response = app_client.get(INBOUND_URL, headers=full_headers)

    assert response.status_code == 200, response.text
    assert "pending" in response.json()
