"""Đối tác, nhân viên và tài khoản ngân hàng qua HTTP (lát 3B-2).

`test_master_data_api.py` đã kiểm bộ sinh route trên một danh mục thuần cây và
một danh mục có cột riêng. Ở đây kiểm ba thứ **chỉ** lát này mới có:

* một bản ghi đối tác hiện ở **cả hai** danh sách (FR-SYS-031) — bằng bộ lọc
  `?flag=`, tức phía máy chủ, vì danh sách có phân trang;
* cột khóa ngoại trỏ sang danh mục khác phải **nhìn thấy được** và không phải
  nút nhóm (`CatalogSpec.references`);
* bảng con tài khoản ngân hàng (FR-SYS-033) với luật "đúng một mặc định".
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    branch_ids,
    catalog_codes,
    create_record,
    ensure_branches,
    ensure_role,
    unique_code,
)
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.partner_bank_account import PartnerBankAccount
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PARTNERS = "partners"
EMPLOYEES = "employees"
PAYMENT_TERMS = "payment_terms"
BANKS = "banks"

EDITOR_ROLE = "ke_toan_doi_tac"
BRANCH_CODES = ["CN_DT_A", "CN_DT_B"]


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def editor_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    every = (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE)
    return ensure_role(
        session_factory,
        dataset_alpha,
        EDITOR_ROLE,
        [
            *catalog_codes(PARTNERS, *every),
            *catalog_codes(EMPLOYEES, *every),
            *catalog_codes(PAYMENT_TERMS, *every),
            *catalog_codes(BANKS, *every),
        ],
    )


@pytest.fixture(scope="module")
def partner_branches(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> list[str]:
    return ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)


@pytest.fixture
def editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    partner_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Kế toán viên của **một** chi nhánh — không phải gửi `X-Branch`."""
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "doitac",
        test_password,
        branch_codes=[partner_branches[0]],
    )


@pytest.fixture
def two_branch_editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    partner_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "doitachai",
        test_password,
        branch_codes=partner_branches,
    )


@pytest.fixture
def bank_id(client: TestClient, editor: dict[str, str]) -> int:
    response = create_record(
        client, editor, BANKS, {"code": unique_code("NH"), "name": "Ngân hàng thử"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _partner(
    client: TestClient,
    headers: dict[str, str],
    *,
    is_customer: bool = True,
    is_vendor: bool = False,
    **extra: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "code": unique_code("DT"),
        "name": "Công ty thử nghiệm",
        "is_customer": is_customer,
        "is_vendor": is_vendor,
        **extra,
    }
    response = create_record(client, headers, PARTNERS, body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _list(
    client: TestClient, headers: dict[str, str], slug: str, **params: object
) -> httpx.Response:
    return client.get(f"/api/v1/master/{slug}", headers=headers, params=params)


def _accounts_url(partner_id: int) -> str:
    return f"/api/v1/master/{PARTNERS}/{partner_id}/bank-accounts"


def _add_account(
    client: TestClient,
    headers: dict[str, str],
    partner_id: int,
    bank_id: int,
    *,
    number: str,
    is_default: bool = False,
) -> httpx.Response:
    """Thêm một tài khoản và khẳng định nó được tạo — trả về phản hồi để đọc tiếp."""
    return client.post(
        _accounts_url(partner_id),
        json={
            "bank_id": bank_id,
            "account_number": number,
            "account_holder": "Công ty thử nghiệm",
            "is_default": is_default,
        },
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )


# ------------------------------------------------------- một bản ghi, hai danh sách


def test_one_partner_shows_up_in_both_lists(client: TestClient, editor: dict[str, str]) -> None:
    """FR-SYS-031: vừa là khách vừa là NCC = **một** bản ghi, hai danh sách.

    Đây là tiêu chí thành công của phase 3 viết thành test. Lọc ở máy chủ chứ
    không ở máy khách vì danh sách có phân trang: trang đầu của "khách hàng"
    phải là trang đầu của khách hàng, không phải phần khách hàng có trong trang
    đầu của tất cả.
    """
    partner = _partner(client, editor, is_customer=True, is_vendor=True)

    customers = _list(client, editor, PARTNERS, flag="customer")
    vendors = _list(client, editor, PARTNERS, flag="vendor")

    assert customers.status_code == 200, customers.text
    assert vendors.status_code == 200, vendors.text
    for response in (customers, vendors):
        ids = [item["id"] for item in response.json()["items"]]
        assert partner["id"] in ids
    assert customers.json()["items"] != [], "danh sách khách hàng không được rỗng"


def test_a_vendor_only_partner_is_absent_from_the_customer_list(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đối trọng của test trên: không có nó thì một bộ lọc **hằng đúng** vẫn xanh."""
    vendor = _partner(client, editor, is_customer=False, is_vendor=True)

    customers = _list(client, editor, PARTNERS, flag="customer", limit=200)

    assert vendor["id"] not in [item["id"] for item in customers.json()["items"]]


def test_the_flag_filter_reaches_the_subtree_read_too(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Cả hai chế độ đọc phải lọc — mở cây từng cấp **và** lấy trọn nhánh.

    Không có test này thì gỡ `flag_column` khỏi một trong hai đường vẫn xanh, và
    màn hình tìm kiếm (dùng `subtree_of`) sẽ trả về nhà cung cấp trong danh sách
    khách hàng.
    """
    group = _partner(client, editor, is_customer=True, is_vendor=True, is_group=True)
    vendor = _partner(client, editor, is_customer=False, is_vendor=True, parent_id=group["id"])
    customer = _partner(client, editor, is_customer=True, is_vendor=False, parent_id=group["id"])

    branch = _list(client, editor, PARTNERS, subtree_of=group["id"], flag="customer")

    ids = [item["id"] for item in branch.json()["items"]]
    assert customer["id"] in ids
    assert vendor["id"] not in ids


def test_an_unknown_flag_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Bỏ qua im lặng nghĩa là danh sách sai mà không ai biết."""
    response = _list(client, editor, PARTNERS, flag="khach")

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "master_data.filter_unknown"


def test_a_catalog_without_flags_refuses_any_flag(
    client: TestClient, editor: dict[str, str]
) -> None:
    response = _list(client, editor, EMPLOYEES, flag="customer")

    assert response.status_code == 422, response.text


def test_a_partner_that_is_neither_customer_nor_vendor_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Bản ghi không thuộc danh sách nào chỉ làm nhiễu ô tra cứu."""
    response = create_record(
        client,
        editor,
        PARTNERS,
        {"code": unique_code("DT"), "name": "Không phải ai cả"},
    )

    assert response.status_code == 422, response.text


def test_a_partner_group_needs_neither_flag(client: TestClient, editor: dict[str, str]) -> None:
    """Nút nhóm chỉ để gom cây nên được miễn — đối trọng của test trên."""
    response = create_record(
        client,
        editor,
        PARTNERS,
        {"code": unique_code("NHOM"), "name": "Nhóm đối tác", "is_group": True},
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------- khóa ngoại sang danh mục khác


def test_a_payment_term_from_another_branch_cannot_be_attached(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    partner_branches: list[str],
) -> None:
    """Khóa ngoại của DB chỉ nói "id có tồn tại" — chưa đủ (FR-SYS-018).

    Không có phép kiểm này, đối tác của chi nhánh A gắn được điều khoản thanh
    toán **riêng** của chi nhánh B: ô đó hiện rỗng trên màn hình của A (mọi
    đường đọc lọc chi nhánh) trong khi hạn nợ vẫn tính theo nó.
    """
    ids = branch_ids(session_factory, dataset_alpha, partner_branches)
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[1]])}
    term = create_record(
        client,
        at_b,
        PAYMENT_TERMS,
        {
            "code": unique_code("DK"),
            "name": "Điều khoản riêng của B",
            "branch_id": ids[partner_branches[1]],
        },
    )
    assert term.status_code == 201, term.text

    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[0]])}
    response = create_record(
        client,
        at_a,
        PARTNERS,
        {
            "code": unique_code("DT"),
            "name": "Đối tác của A",
            "is_customer": True,
            "payment_term_id": term.json()["id"],
        },
    )

    assert response.status_code == 404, response.text


def test_a_payment_term_group_cannot_be_attached(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Nhóm để gom và cộng tổng, không mang giá trị nào để tính hạn nợ."""
    group = create_record(
        client,
        editor,
        PAYMENT_TERMS,
        {"code": unique_code("DKN"), "name": "Nhóm điều khoản", "is_group": True},
    )
    assert group.status_code == 201, group.text

    response = create_record(
        client,
        editor,
        PARTNERS,
        {
            "code": unique_code("DT"),
            "name": "Đối tác",
            "is_customer": True,
            "payment_term_id": group.json()["id"],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "master_data.group_not_postable"


def test_the_reference_check_runs_on_update_too(client: TestClient, editor: dict[str, str]) -> None:
    """Đường sửa cũng phải kiểm — nếu không thì tạo sạch rồi sửa bẩn."""
    partner = _partner(client, editor, is_customer=True)
    group = create_record(
        client,
        editor,
        PAYMENT_TERMS,
        {"code": unique_code("DKN"), "name": "Nhóm điều khoản", "is_group": True},
    )

    response = client.put(
        f"/api/v1/master/{PARTNERS}/{partner['id']}",
        json={
            "row_version": partner["row_version"],
            "code": partner["code"],
            "name": partner["name"],
            "is_active": True,
            "is_customer": True,
            "is_vendor": False,
            "is_organization": True,
            "payment_term_id": group.json()["id"],
        },
        headers=editor,
    )

    assert response.status_code == 422, response.text


def test_a_usable_payment_term_is_accepted(client: TestClient, editor: dict[str, str]) -> None:
    """Đối trọng: phép kiểm không được chặn đường dùng bình thường."""
    term = create_record(
        client,
        editor,
        PAYMENT_TERMS,
        {"code": unique_code("DK"), "name": "30 ngày", "due_days": 30},
    )
    assert term.status_code == 201, term.text

    partner = _partner(client, editor, payment_term_id=term.json()["id"], credit_limit="5000000")

    assert partner["payment_term_id"] == term.json()["id"]


# ------------------------------------------------------------------------ nhân viên


def test_an_employee_bank_account_without_a_bank_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Nửa thông tin tệ hơn không có: màn hình hiện như đã khai xong."""
    response = create_record(
        client,
        editor,
        EMPLOYEES,
        {
            "code": unique_code("NV"),
            "name": "Nguyễn Văn A",
            "bank_account_number": "0123456789",
        },
    )

    assert response.status_code == 422, response.text
    # Validator Pydantic tồn tại để người dùng nhận **câu nói việc phải làm**,
    # không phải tên một ràng buộc DB. `CHECK` phía dưới cũng trả `422`, nên
    # không có khẳng định về nội dung thì gỡ validator đi test vẫn xanh — đúng
    # điểm mù L3 mà review chỉ ra.
    body = response.json()
    assert body["error_code"] == "request.validation_failed", body
    reported = " ".join(str(error.get("msg", "")) for error in body.get("errors", []))
    assert "ngân hàng phải khai cùng nhau" in reported, body


def test_an_employee_keeps_its_identity_fields(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    response = create_record(
        client,
        editor,
        EMPLOYEES,
        {
            "code": unique_code("NV"),
            "name": "Trần Thị B",
            "department": "Phòng kế toán",
            "position": "Kế toán trưởng",
            "id_number": "079123456789",
            "tax_code": "8123456789",
            "bank_id": bank_id,
            "bank_account_number": "0987654321",
        },
    )

    assert response.status_code == 201, response.text
    record = response.json()
    assert record["department"] == "Phòng kế toán"
    assert record["bank_id"] == bank_id


def test_an_employee_bank_from_another_branch_is_refused(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    partner_branches: list[str],
) -> None:
    ids = branch_ids(session_factory, dataset_alpha, partner_branches)
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[1]])}
    bank = create_record(
        client,
        at_b,
        BANKS,
        {
            "code": unique_code("NH"),
            "name": "Ngân hàng riêng của B",
            "branch_id": ids[partner_branches[1]],
        },
    )
    assert bank.status_code == 201, bank.text

    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[0]])}
    response = create_record(
        client,
        at_a,
        EMPLOYEES,
        {
            "code": unique_code("NV"),
            "name": "Nhân viên của A",
            "bank_id": bank.json()["id"],
            "bank_account_number": "111222333",
        },
    )

    assert response.status_code == 404, response.text


# ------------------------------------------------------ tài khoản ngân hàng đối tác


def test_the_first_account_becomes_the_default_without_being_asked(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Một đối tác có đúng một tài khoản thì không có gì để chọn (FR-SYS-033)."""
    partner = _partner(client, editor)

    response = _add_account(client, editor, int(partner["id"]), bank_id, number="1000001")

    assert response.status_code == 201, response.text
    assert response.json()["is_default"] is True


def test_setting_another_account_as_default_clears_the_previous_one(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Đúng một mặc định cho mỗi đối tác — chỉ mục có điều kiện canh dưới DB."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    first = _add_account(client, editor, partner_id, bank_id, number="2000001")
    second = _add_account(client, editor, partner_id, bank_id, number="2000002")
    assert second.status_code == 201, second.text

    promoted = client.put(
        f"{_accounts_url(partner_id)}/{second.json()['id']}",
        json={
            "row_version": second.json()["row_version"],
            "bank_id": bank_id,
            "account_number": "2000002",
            "account_holder": "Công ty thử nghiệm",
            "is_default": True,
            "is_active": True,
        },
        headers=editor,
    )

    assert promoted.status_code == 200, promoted.text
    listing = client.get(_accounts_url(partner_id), headers=editor).json()["items"]
    defaults = [item["id"] for item in listing if item["is_default"]]
    assert defaults == [second.json()["id"]]
    assert first.json()["id"] not in defaults


def test_closing_the_default_account_drops_the_default_flag(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Ủy nhiệm chi kỳ sau không được lấy sẵn một tài khoản đã đóng."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    account = _add_account(client, editor, partner_id, bank_id, number="3000001").json()

    closed = client.put(
        f"{_accounts_url(partner_id)}/{account['id']}",
        json={
            "row_version": account["row_version"],
            "bank_id": bank_id,
            "account_number": account["account_number"],
            "account_holder": account["account_holder"],
            "is_default": True,
            "is_active": False,
        },
        headers=editor,
    )

    assert closed.status_code == 200, closed.text
    assert closed.json()["is_default"] is False


def test_deleting_the_default_account_promotes_another_one(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Còn tài khoản hoạt động thì luôn còn **đúng một** mặc định (FR-SYS-033).

    Không có bước đôn lên thì sau một lần xóa, đối tác rơi lại đúng trạng thái mà
    `add()` được dựng ra để tránh: có tài khoản nhưng không cái nào chọn sẵn, và
    ủy nhiệm chi ở phase 6 lại phải hỏi một câu chỉ có một câu trả lời.
    """
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    first = _add_account(client, editor, partner_id, bank_id, number="7100001").json()
    second = _add_account(client, editor, partner_id, bank_id, number="7100002").json()
    assert first["is_default"] is True

    removed = client.delete(f"{_accounts_url(partner_id)}/{first['id']}", headers=editor)

    assert removed.status_code == 204, removed.text
    listing = client.get(_accounts_url(partner_id), headers=editor).json()["items"]
    assert [item["id"] for item in listing if item["is_default"]] == [second["id"]]


def test_closing_the_default_account_promotes_another_one(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Đóng tài khoản mặc định cũng phải đôn cái khác lên — cùng bất biến, đường ghi khác."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    first = _add_account(client, editor, partner_id, bank_id, number="7200001").json()
    second = _add_account(client, editor, partner_id, bank_id, number="7200002").json()

    closed = client.put(
        f"{_accounts_url(partner_id)}/{first['id']}",
        json={
            "row_version": first["row_version"],
            "bank_id": bank_id,
            "account_number": first["account_number"],
            "account_holder": first["account_holder"],
            "is_default": True,
            "is_active": False,
        },
        headers=editor,
    )

    assert closed.status_code == 200, closed.text
    listing = client.get(_accounts_url(partner_id), headers=editor).json()["items"]
    assert [item["id"] for item in listing if item["is_default"]] == [second["id"]]


def test_a_partner_with_only_closed_accounts_has_no_default(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Đối trọng: không còn tài khoản hoạt động thì **không** đôn tài khoản đã đóng lên."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    only = _add_account(client, editor, partner_id, bank_id, number="7300001").json()

    client.put(
        f"{_accounts_url(partner_id)}/{only['id']}",
        json={
            "row_version": only["row_version"],
            "bank_id": bank_id,
            "account_number": only["account_number"],
            "account_holder": only["account_holder"],
            "is_default": False,
            "is_active": False,
        },
        headers=editor,
    )

    listing = client.get(
        _accounts_url(partner_id), headers=editor, params={"include_inactive": True}
    ).json()["items"]
    assert [item["is_default"] for item in listing] == [False]


def test_a_closed_account_is_hidden_unless_asked_for(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Ô chọn trên ủy nhiệm chi dựng từ đường đọc này — tài khoản đã đóng không được hiện."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    kept = _add_account(client, editor, partner_id, bank_id, number="7400001").json()
    closed = _add_account(client, editor, partner_id, bank_id, number="7400002").json()
    client.put(
        f"{_accounts_url(partner_id)}/{closed['id']}",
        json={
            "row_version": closed["row_version"],
            "bank_id": bank_id,
            "account_number": closed["account_number"],
            "account_holder": closed["account_holder"],
            "is_default": False,
            "is_active": False,
        },
        headers=editor,
    )

    visible = client.get(_accounts_url(partner_id), headers=editor).json()["items"]
    everything = client.get(
        _accounts_url(partner_id), headers=editor, params={"include_inactive": True}
    ).json()["items"]

    assert [item["id"] for item in visible] == [kept["id"]]
    assert {item["id"] for item in everything} == {kept["id"], closed["id"]}


def test_a_bank_of_another_branch_cannot_be_used_on_a_bank_account(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    partner_branches: list[str],
) -> None:
    """Bảng con đi qua cùng phép kiểm với danh mục (`ensure_catalog_choice`).

    Ở danh mục thì phép kiểm này có test (điều khoản thanh toán, ngân hàng của
    nhân viên); ở bảng con thì trước đây không — gỡ nó ra là đối tác chi nhánh A
    gắn được ngân hàng riêng của chi nhánh B mà không gì báo (review H4).
    """
    ids = branch_ids(session_factory, dataset_alpha, partner_branches)
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[1]])}
    far_bank = create_record(
        client,
        at_b,
        BANKS,
        {
            "code": unique_code("NH"),
            "name": "Ngân hàng riêng của B",
            "branch_id": ids[partner_branches[1]],
        },
    )
    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[0]])}
    partner = _partner(client, at_a)

    response = client.post(
        _accounts_url(int(partner["id"])),
        json={
            "bank_id": far_bank.json()["id"],
            "account_number": "7500001",
            "account_holder": "Công ty thử nghiệm",
            "is_default": False,
        },
        headers={**at_a, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )

    assert response.status_code == 404, response.text


def test_the_bank_check_also_runs_when_updating_a_bank_account(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    partner_branches: list[str],
) -> None:
    """Đường sửa cũng phải kiểm — nếu không thì thêm sạch rồi sửa bẩn."""
    ids = branch_ids(session_factory, dataset_alpha, partner_branches)
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[1]])}
    far_bank = create_record(
        client,
        at_b,
        BANKS,
        {
            "code": unique_code("NH"),
            "name": "Ngân hàng riêng của B",
            "branch_id": ids[partner_branches[1]],
        },
    )
    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[0]])}
    own_bank = create_record(
        client, at_a, BANKS, {"code": unique_code("NH"), "name": "Ngân hàng dùng chung"}
    )
    partner = _partner(client, at_a)
    partner_id = int(partner["id"])
    account = _add_account(
        client, at_a, partner_id, int(own_bank.json()["id"]), number="7600001"
    ).json()

    response = client.put(
        f"{_accounts_url(partner_id)}/{account['id']}",
        json={
            "row_version": account["row_version"],
            "bank_id": far_bank.json()["id"],
            "account_number": account["account_number"],
            "account_holder": account["account_holder"],
            "is_default": True,
            "is_active": True,
        },
        headers=at_a,
    )

    assert response.status_code == 404, response.text


def test_saving_a_bank_account_on_a_stale_version_is_refused(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Khóa lạc quan cũng phải có ở bảng con (FR-NFR-005)."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    account = _add_account(client, editor, partner_id, bank_id, number="7700001").json()
    body = {
        "row_version": account["row_version"],
        "bank_id": bank_id,
        "account_number": account["account_number"],
        "account_holder": "Tên mới",
        "is_default": True,
        "is_active": True,
    }
    first = client.put(f"{_accounts_url(partner_id)}/{account['id']}", json=body, headers=editor)
    assert first.status_code == 200, first.text

    second = client.put(f"{_accounts_url(partner_id)}/{account['id']}", json=body, headers=editor)

    assert second.status_code == 409, second.text


def test_adding_a_second_default_account_clears_the_first(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Đường **thêm** cũng phải gỡ mặc định cũ, không chỉ đường sửa."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    first = _add_account(client, editor, partner_id, bank_id, number="7800001").json()
    second = _add_account(
        client, editor, partner_id, bank_id, number="7800002", is_default=True
    ).json()

    listing = client.get(_accounts_url(partner_id), headers=editor).json()["items"]

    assert [item["id"] for item in listing if item["is_default"]] == [second["id"]]
    assert first["id"] not in [item["id"] for item in listing if item["is_default"]]


def test_an_account_of_another_partner_is_not_reachable_through_this_partner(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    """Đường dẫn mang hai id; không đối chiếu chúng là một đường đọc vòng."""
    owner = _partner(client, editor)
    stranger = _partner(client, editor)
    account = _add_account(client, editor, int(owner["id"]), bank_id, number="4000001").json()

    response = client.delete(
        f"{_accounts_url(int(stranger['id']))}/{account['id']}", headers=editor
    )

    assert response.status_code == 404, response.text


def test_bank_accounts_of_a_partner_in_another_branch_are_not_reachable(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    partner_branches: list[str],
    bank_id: int,
) -> None:
    """Phạm vi chi nhánh của bảng con là phạm vi của đối tác chủ."""
    ids = branch_ids(session_factory, dataset_alpha, partner_branches)
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[1]])}
    partner = _partner(client, at_b, branch_id=ids[partner_branches[1]])

    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[partner_branches[0]])}
    response = client.get(_accounts_url(int(partner["id"])), headers=at_a)

    assert response.status_code == 404, response.text


def test_a_partner_group_has_no_bank_accounts(client: TestClient, editor: dict[str, str]) -> None:
    group = create_record(
        client,
        editor,
        PARTNERS,
        {"code": unique_code("NHOM"), "name": "Nhóm đối tác", "is_group": True},
    )

    response = client.get(_accounts_url(int(group.json()["id"])), headers=editor)

    assert response.status_code == 404, response.text


def test_deleting_a_partner_takes_its_bank_accounts_with_it(
    client: TestClient,
    editor: dict[str, str],
    bank_id: int,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Tài khoản **thuộc về** đối tác, không tồn tại độc lập (`ON DELETE CASCADE`)."""
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    _add_account(client, editor, partner_id, bank_id, number="5000001")

    deleted = client.delete(f"/api/v1/master/{PARTNERS}/{partner_id}", headers=editor)
    assert deleted.status_code == 204, deleted.text

    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        leftovers = session.scalars(
            select(PartnerBankAccount.id).where(PartnerBankAccount.partner_id == partner_id)
        ).all()
    assert leftovers == []


def test_adding_an_account_repeats_the_idempotency_key_without_adding_twice(
    client: TestClient, editor: dict[str, str], bank_id: int
) -> None:
    partner = _partner(client, editor)
    partner_id = int(partner["id"])
    key = unique_code("KEY")
    body = {
        "bank_id": bank_id,
        "account_number": "6000001",
        "account_holder": "Công ty thử nghiệm",
        "is_default": False,
    }

    first = client.post(
        _accounts_url(partner_id), json=body, headers={**editor, IDEMPOTENCY_HEADER: key}
    )
    second = client.post(
        _accounts_url(partner_id), json=body, headers={**editor, IDEMPOTENCY_HEADER: key}
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    listing = client.get(_accounts_url(partner_id), headers=editor).json()["items"]
    assert len(listing) == 1
