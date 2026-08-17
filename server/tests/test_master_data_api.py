"""Danh mục đầu-cuối qua HTTP — route sinh từ registry (lát 3B-1, H47).

`test_master_data_registry.py` đã chứng minh **hình dạng** đúng: mọi danh mục có
đủ sáu route, đủ bốn mã quyền, và `extra_fields` khớp cột. Ở đây kiểm phần mà
hình dạng không nói được — hành vi của chuỗi quyền → dataset → phạm vi chi nhánh
→ khóa lạc quan → idempotency, trên một danh mục **thuần cây** và một danh mục
**có cột riêng**.

Hai danh mục chứ không mười bảy, có chủ đích: bộ sinh route là một đoạn mã, và
chạy nó qua hai hình dạng đầu vào khác nhau chứng minh được đúng chỗ nó có thể
rẽ nhánh. Chạy cả mười bảy chỉ nhân thời gian với mười bảy để kiểm lại cùng một
đoạn mã.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
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
from ket.api.routers.master_data import MAX_PAGE_SIZE
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.usage import record_use
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.permissions import MASTER_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PLAIN = "warehouses"
"""Danh mục thuần cây — không cột riêng nào."""

SECOND_PLAIN = "units_of_measure"
"""Danh mục thuần cây **thứ hai** — chỉ để chứng minh phạm vi khóa idempotency
tách theo từng danh mục (M-5)."""

WITH_EXTRAS = "payment_terms"
"""Danh mục có ba cột riêng **và** một validator liên-trường."""

DIMENSION = "analysis_dimension"
"""Chiều phân tích mở rộng — cùng phân hệ quyền `master`, cùng nhóm màn hình 07.

Test HTTP của nó nằm chung tệp này chứ không tách riêng: nó dùng đúng bộ khung
người-dùng-có-vai-trò-và-chi-nhánh ở trên, và chép bộ khung ấy sang một tệp thứ
hai là cách hai bản sao lệch nhau ở lát sau. Luật **nghiệp vụ** của chiều phân
tích thì có tệp riêng (`test_analysis_dimensions.py`) — ở đây chỉ kiểm phần đi
qua HTTP."""

EDITOR_ROLE = "ke_toan_danh_muc"
READER_ROLE = "chi_xem_danh_muc"

BRANCH_CODES = ["CN_DM_A", "CN_DM_B"]


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def editor_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        EDITOR_ROLE,
        [
            *catalog_codes(PLAIN, Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE),
            *catalog_codes(WITH_EXTRAS, Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE),
            *catalog_codes(SECOND_PLAIN, Action.VIEW, Action.CREATE),
            permission_code(MASTER_MODULE, DIMENSION, Action.VIEW),
            permission_code(MASTER_MODULE, DIMENSION, Action.CREATE),
        ],
    )


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Chỉ `view` — dùng để chứng minh xem được không có nghĩa là sửa được."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        READER_ROLE,
        [*catalog_codes(PLAIN, Action.VIEW), *catalog_codes(WITH_EXTRAS, Action.VIEW)],
    )


@pytest.fixture(scope="module")
def catalog_branches(
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
    catalog_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Kế toán viên của **một** chi nhánh — nên không phải gửi `X-Branch`."""
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "danhmuc",
        test_password,
        branch_codes=[catalog_branches[0]],
    )


@pytest.fixture
def reader(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    reader_role: str,
    catalog_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        reader_role,
        "chixem",
        test_password,
        branch_codes=[catalog_branches[0]],
    )


@pytest.fixture
def two_branch_editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    catalog_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Kế toán viên được gán **cả hai** chi nhánh."""
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "caohai",
        test_password,
        branch_codes=catalog_branches,
    )


@pytest.fixture
def both_branches_record(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> dict[str, object]:
    """Một bản ghi **riêng** của chi nhánh B, tạo bởi người được gán chi nhánh B.

    Phải đi qua người có phạm vi gồm B: từ lát này, tạo danh mục riêng cho một
    chi nhánh ngoài phạm vi của mình bị chặn — xem
    `test_creating_a_record_for_a_branch_outside_the_scope_is_refused`.
    """
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    # `X-Branch` phải trỏ đúng chi nhánh B: phạm vi **ghi** bằng phạm vi **đọc**
    # (xem `_ensure_branch_in_scope`), nên tạo bản ghi riêng cho B đòi người thực
    # hiện đang thao tác ở B — không chỉ "được gán cả A lẫn B".
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[catalog_branches[1]])}
    response = create_record(
        client,
        at_b,
        PLAIN,
        {
            "code": unique_code("KZ"),
            "name": "Kho của chi nhánh B",
            "branch_id": ids[catalog_branches[1]],
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# --------------------------------------------------------------- vòng đời cơ bản


def test_create_read_update_delete_on_a_plain_catalog(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Vòng đời đầy đủ trên danh mục không có cột riêng nào."""
    code = unique_code("KHO")
    created = create_record(client, editor, PLAIN, {"code": code, "name": "Kho trung tâm"})
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["code"] == code
    assert record["level"] == 1
    assert record["path"] == f"{record['id']}."
    assert record["row_version"] == 1
    assert record["is_active"] is True

    fetched = client.get(f"/api/v1/master/{PLAIN}/{record['id']}", headers=editor)
    assert fetched.status_code == 200
    assert fetched.json()["uid"] == record["uid"]

    updated = client.put(
        f"/api/v1/master/{PLAIN}/{record['id']}",
        json={
            "row_version": record["row_version"],
            "code": code,
            "name": "Kho trung tâm (đã đổi tên)",
            "name_en": "Central warehouse",
            "is_active": False,
        },
        headers=editor,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name_en"] == "Central warehouse"
    assert updated.json()["is_active"] is False
    # `uid` **không** đổi khi mọi thứ khác đổi (RT-19) — lý do cột này tồn tại.
    assert updated.json()["uid"] == record["uid"]

    deleted = client.delete(f"/api/v1/master/{PLAIN}/{record['id']}", headers=editor)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/master/{PLAIN}/{record['id']}", headers=editor).status_code == 404


def test_extra_columns_round_trip_through_the_generated_routes(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Cột riêng đi vào và đi ra đúng giá trị — đây là lý do H47 chọn model riêng.

    `discount_percent` khẳng định bằng `Decimal`: một `float` lọt vào đường này
    sẽ trả về `2.0000000000000004` ở đâu đó, và con số ấy nhân vào tiền.
    """
    code = unique_code("DKTT")
    created = create_record(
        client,
        editor,
        WITH_EXTRAS,
        {
            "code": code,
            "name": "2/10 net 30",
            "due_days": 30,
            "discount_days": 10,
            "discount_percent": "2.5",
        },
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["due_days"] == 30
    assert record["discount_days"] == 10
    assert Decimal(str(record["discount_percent"])) == Decimal("2.5")

    updated = client.put(
        f"/api/v1/master/{WITH_EXTRAS}/{record['id']}",
        json={
            "row_version": record["row_version"],
            "code": code,
            "name": "net 45",
            "name_en": None,
            "is_active": True,
            "due_days": 45,
            "discount_days": 0,
            "discount_percent": "0",
        },
        headers=editor,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["due_days"] == 45
    assert Decimal(str(updated.json()["discount_percent"])) == Decimal(0)


def test_the_cross_field_rule_of_a_catalog_is_enforced_by_the_api(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Validator khai cạnh model ORM phải còn sống sau khi `create_model` kế thừa nó.

    Đây là điều `build_schemas` dựa vào khi đưa cột riêng vào bằng **kế thừa**
    thay vì sao chép trường. Sao chép sẽ để luật này lại phía sau và không gì
    báo — model vẫn dựng được, vẫn nhận đúng các trường.
    """
    response = create_record(
        client,
        editor,
        WITH_EXTRAS,
        {
            "code": unique_code("DKXAU"),
            "name": "Cửa sổ chiết khấu dài hơn hạn nợ",
            "due_days": 10,
            "discount_days": 30,
            "discount_percent": "1",
        },
    )
    assert response.status_code == 422, response.text

    # Thân phản hồi phải **dựng được**, không chỉ mang đúng mã trạng thái.
    # Pydantic v2 gắn chính đối tượng `ValueError` vào `ctx["error"]` cho lỗi
    # `value_error`, và `json.dumps` không mã hóa được nó — trước khi
    # `problem_details._serializable_errors` ra đời, luật liên-trường đầu tiên
    # của repo biến một lỗi nhập liệu thành `500`.
    body = response.json()
    assert body["error_code"] == "request.validation_failed"
    assert any("chiết khấu" in error["msg"] for error in body["errors"])


def test_unknown_fields_are_refused_instead_of_dropped(
    client: TestClient, editor: dict[str, str]
) -> None:
    """`extra="forbid"`: gõ sai tên trường phải đỏ, không được im lặng bỏ qua.

    Bỏ qua im lặng nghĩa là người dùng thấy form lưu thành công còn giá trị vừa
    nhập thì biến mất ở lần tải lại.
    """
    response = create_record(
        client,
        editor,
        WITH_EXTRAS,
        {"code": unique_code("DKSAI"), "name": "Sai tên trường", "due_dayz": 30},
    )
    assert response.status_code == 422


def test_a_plain_catalog_refuses_the_extra_fields_of_another_catalog(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Model request là **của từng danh mục**, không phải một model chung.

    Nếu bộ sinh trượt về một model chung thì `due_days` sẽ được nhận ở đây và
    lặng lẽ rơi đi.
    """
    response = create_record(
        client, editor, PLAIN, {"code": unique_code("KHO"), "name": "Kho", "due_days": 30}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- cây


def test_moving_a_node_rewrites_the_whole_subtree(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chuyển nhánh cập nhật `path`/`level` của **con cháu**, không chỉ của nút được chọn."""
    root_a = create_record(
        client, editor, PLAIN, {"code": unique_code("KA"), "name": "Nhóm A", "is_group": True}
    ).json()
    root_b = create_record(
        client, editor, PLAIN, {"code": unique_code("KB"), "name": "Nhóm B", "is_group": True}
    ).json()
    child = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KC"), "name": "Kho con", "parent_id": root_a["id"]},
    ).json()
    grandchild = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KD"), "name": "Ngăn", "parent_id": child["id"]},
    ).json()
    assert grandchild["level"] == 3

    moved = client.put(
        f"/api/v1/master/{PLAIN}/{child['id']}/parent",
        json={"row_version": child["row_version"], "new_parent_id": root_b["id"]},
        headers=editor,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["path"] == f"{root_b['id']}.{child['id']}."

    after = client.get(f"/api/v1/master/{PLAIN}/{grandchild['id']}", headers=editor).json()
    assert after["path"] == f"{root_b['id']}.{child['id']}.{grandchild['id']}."
    assert after["level"] == 3
    # `row_version` của con cháu **cũng** tăng: một client đang giữ bản cũ đã cầm
    # giá trị lỗi thời, và để nó lưu đè im lặng là đúng thứ khóa lạc quan chặn.
    assert after["row_version"] > grandchild["row_version"]


def test_a_node_cannot_be_moved_into_its_own_subtree(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Kéo-thả sai của người dùng, không phải lỗi lập trình — nên là lỗi nghiệp vụ có mã."""
    root = create_record(
        client, editor, PLAIN, {"code": unique_code("KV"), "name": "Nhóm", "is_group": True}
    ).json()
    child = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KW"), "name": "Con", "parent_id": root["id"]},
    ).json()

    response = client.put(
        f"/api/v1/master/{PLAIN}/{root['id']}/parent",
        json={"row_version": root["row_version"], "new_parent_id": child["id"]},
        headers=editor,
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "master_data.parent_cycle"


def test_listing_walks_the_tree_two_ways(client: TestClient, editor: dict[str, str]) -> None:
    """`parent_id` mở dần từng cấp; `subtree_of` lấy trọn nhánh."""
    root = create_record(
        client, editor, PLAIN, {"code": unique_code("KL"), "name": "Gốc", "is_group": True}
    ).json()
    child = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KM"), "name": "Con", "parent_id": root["id"]},
    ).json()
    create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KN"), "name": "Cháu", "parent_id": child["id"]},
    )

    children = client.get(
        f"/api/v1/master/{PLAIN}", params={"parent_id": root["id"]}, headers=editor
    ).json()["items"]
    assert [item["id"] for item in children] == [child["id"]]

    subtree = client.get(
        f"/api/v1/master/{PLAIN}", params={"subtree_of": root["id"]}, headers=editor
    ).json()["items"]
    assert len(subtree) == 3
    # `ORDER BY path` = thứ tự duyệt trước, thứ mà màn hình cây cần.
    assert [item["level"] for item in subtree] == [1, 2, 3]


# ------------------------------------------------------- quyền & phạm vi


def test_view_permission_does_not_grant_write(client: TestClient, reader: dict[str, str]) -> None:
    """Bốn hành vi tách nhau, và mỗi danh mục có bộ riêng (H48)."""
    response = create_record(client, reader, PLAIN, {"code": unique_code("KX"), "name": "Kho"})
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.permission_denied"


def test_permission_is_per_catalog_not_shared(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    catalog_branches: list[str],
    test_password: str,
) -> None:
    """Sửa được danh mục kho **không** đồng nghĩa sửa được điều khoản thanh toán.

    Đây là toàn bộ nội dung của H48. Với một mã quyền chung `master.*`, người
    được thêm một kho cũng đổi được điều kiện công nợ của cả doanh nghiệp.
    """
    role = ensure_role(
        session_factory,
        dataset_alpha,
        "chi_sua_kho",
        catalog_codes(PLAIN, Action.VIEW, Action.CREATE),
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "chisuakho",
        test_password,
        branch_codes=[catalog_branches[0]],
    )

    assert (
        create_record(
            client, headers, PLAIN, {"code": unique_code("KY"), "name": "Kho"}
        ).status_code
        == 201
    )
    assert (
        create_record(
            client,
            headers,
            WITH_EXTRAS,
            {"code": unique_code("DK"), "name": "Điều khoản", "due_days": 7},
        ).status_code
        == 403
    )


def test_a_record_of_another_branch_is_invisible(
    client: TestClient,
    editor: dict[str, str],
    both_branches_record: dict[str, object],
) -> None:
    """Danh mục **riêng** của chi nhánh khác trả `404`, không phải `403` (H43).

    `403` là lời xác nhận rằng bản ghi đó tồn tại, và một vòng lặp qua id sẽ vẽ
    lại được danh mục của chi nhánh bên cạnh.
    """
    record_id = both_branches_record["id"]

    # Người thực hiện chỉ thuộc chi nhánh A.
    assert client.get(f"/api/v1/master/{PLAIN}/{record_id}", headers=editor).status_code == 404

    listed = client.get(f"/api/v1/master/{PLAIN}", headers=editor).json()["items"]
    assert record_id not in {item["id"] for item in listed}


def test_creating_a_record_for_a_branch_outside_the_scope_is_refused(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> None:
    """`branch_id` đến từ thân request, nên nó phải bị kiểm như mọi đầu vào khác.

    Không kiểm thì bất kỳ ai có quyền tạo danh mục cũng cắm được bản ghi vào
    ngăn của chi nhánh khác — và vì chính họ không nhìn thấy nó sau đó, bản ghi
    ấy thành thứ chỉ chi nhánh kia thấy mà không ai bên đó tạo ra.
    """
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    response = create_record(
        client,
        editor,
        PLAIN,
        {
            "code": unique_code("KNGOAI"),
            "name": "Kho cắm vào chi nhánh khác",
            "branch_id": ids[catalog_branches[1]],
        },
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"


def test_shared_records_are_visible_from_every_branch(
    client: TestClient, editor: dict[str, str]
) -> None:
    """`branch_id IS NULL` = dùng chung toàn công ty (FR-SYS-018).

    Đây là mặt còn lại của test trên, và là lý do bảng danh mục cố ý không bật
    RLS (H39): policy chi nhánh sẽ giấu đúng những dòng `NULL` này khỏi mọi
    người.
    """
    shared = create_record(
        client, editor, PLAIN, {"code": unique_code("KCHUNG"), "name": "Kho dùng chung"}
    ).json()
    assert shared["branch_id"] is None

    listed = client.get(f"/api/v1/master/{PLAIN}", headers=editor).json()["items"]
    assert shared["id"] in {item["id"] for item in listed}


def test_the_branch_scope_cannot_be_widened_from_the_query_string(
    client: TestClient,
    editor: dict[str, str],
    both_branches_record: dict[str, object],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> None:
    """Không có tham số `branch_id` trên đường đọc — sửa URL không mở rộng tầm nhìn."""
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)

    listed = client.get(
        f"/api/v1/master/{PLAIN}",
        params={"branch_id": ids[catalog_branches[1]]},
        headers=editor,
    ).json()["items"]
    assert both_branches_record["id"] not in {item["id"] for item in listed}


def test_the_acting_branch_header_selects_which_private_records_are_visible(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    catalog_branches: list[str],
    test_password: str,
) -> None:
    """Người dùng nhiều chi nhánh thấy đúng chi nhánh **đang thao tác**."""
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "haichinhanh",
        test_password,
        branch_codes=catalog_branches,
    )
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    at_b = {**headers, BRANCH_HEADER: str(ids[catalog_branches[1]])}

    record = create_record(
        client,
        at_b,
        PLAIN,
        {
            "code": unique_code("KR"),
            "name": "Kho B",
            "branch_id": ids[catalog_branches[1]],
        },
    ).json()

    at_a = {**headers, BRANCH_HEADER: str(ids[catalog_branches[0]])}
    assert client.get(f"/api/v1/master/{PLAIN}/{record['id']}", headers=at_b).status_code == 200
    assert client.get(f"/api/v1/master/{PLAIN}/{record['id']}", headers=at_a).status_code == 404


# ------------------------------------------------- idempotency & khóa lạc quan


def test_replaying_a_create_returns_the_same_record(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Mất phản hồi rồi bấm lại không được tạo bản ghi thứ hai (FR-NFR-004).

    `200` chứ không `201` ở lần thứ hai: mã trạng thái là chỗ duy nhất client
    biết được lần này có tạo thêm gì hay không.
    """
    key = uuid4().hex
    body = {"code": unique_code("KI"), "name": "Kho gửi lại"}

    first = create_record(client, editor, PLAIN, body, key=key)
    second = create_record(client, editor, PLAIN, body, key=key)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_creating_without_an_idempotency_key_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Cổng `test_idempotency_route_coverage` canh việc *khai*; đây canh việc *ép*."""
    response = client.post(
        f"/api/v1/master/{PLAIN}",
        json={"code": unique_code("KJ"), "name": "Không khóa"},
        headers=editor,
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_a_stale_row_version_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Hai người cùng sửa một bản ghi — người thứ hai nhận `409`, không ghi đè (FR-NFR-005)."""
    record = create_record(client, editor, PLAIN, {"code": unique_code("KK"), "name": "Kho"}).json()
    body = {
        "row_version": record["row_version"],
        "code": record["code"],
        "name": "Sửa lần một",
        "name_en": None,
        "is_active": True,
    }
    assert (
        client.put(f"/api/v1/master/{PLAIN}/{record['id']}", json=body, headers=editor).status_code
        == 200
    )

    body["name"] = "Sửa lần hai bằng phiên bản cũ"
    conflict = client.put(f"/api/v1/master/{PLAIN}/{record['id']}", json=body, headers=editor)
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "concurrency.row_version_conflict"


# ------------------------------------------------------------------ xóa


def test_a_record_in_use_cannot_be_deleted(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """BR-SYS-02: đã lên chứng từ thì dùng "Ngừng theo dõi", không xóa.

    Ghi thẳng bộ đếm thay vì dựng một chứng từ: chứng từ đầu tiên ra đời ở phase
    6, còn luật cần kiểm ở đây là luật của **bộ đếm** — nó đã là cổng duy nhất
    mà `delete` hỏi.
    """
    record = create_record(client, editor, PLAIN, {"code": unique_code("KU"), "name": "Kho"}).json()
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        record_use(session, entity_type="warehouses", entity_id=record["id"])

    response = client.delete(f"/api/v1/master/{PLAIN}/{record['id']}", headers=editor)
    assert response.status_code == 409
    assert response.json()["error_code"] == "master_data.in_use"
    assert response.json()["details"]["usage_count"] == 1


def test_a_group_with_children_cannot_be_deleted(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chuyển nhánh con đi trước — và chính lúc đó người dùng thấy mình có bao nhiêu thứ."""
    root = create_record(
        client, editor, PLAIN, {"code": unique_code("KG"), "name": "Nhóm", "is_group": True}
    ).json()
    create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KH"), "name": "Con", "parent_id": root["id"]},
    )

    response = client.delete(f"/api/v1/master/{PLAIN}/{root['id']}", headers=editor)
    assert response.status_code == 409
    assert response.json()["error_code"] == "master_data.in_use"


def test_an_unknown_catalog_slug_has_no_route(client: TestClient, editor: dict[str, str]) -> None:
    """Danh mục không đăng ký = không có đường dẫn nào cả.

    Khác hẳn một router `{type}` generic, nơi slug lạ sẽ vào tới thân hàm rồi
    mới bị từ chối. Ở đây nó không tồn tại với FastAPI ngay từ bảng route.
    """
    assert client.get("/api/v1/master/khong_co_that", headers=editor).status_code == 404


# ------------------------------------------------ chiều phân tích qua HTTP


def _declare_dimension(
    client: TestClient, headers: dict[str, str], body: dict[str, object]
) -> object:
    return client.post(
        "/api/v1/dimensions", json=body, headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex}
    )


def test_the_builtin_statistical_dimension_is_listed(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chiều dựng sẵn (FR-SYS-051) đọc được qua API ngay, không cần khai gì thêm."""
    response = client.get("/api/v1/dimensions", headers=editor)

    assert response.status_code == 200, response.text
    assert "STAT" in {item["code"] for item in response.json()["items"]}


def test_declaring_a_dimension_and_its_values_over_http(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đường mà tiêu chí "khai bằng cấu hình, không sửa code" đi qua ở v1."""
    code = f"KENH{uuid4().hex[:6].upper()}"
    declared = _declare_dimension(client, editor, {"code": code, "name": "Kênh bán hàng"})
    assert declared.status_code == 201, declared.text

    added = client.post(
        f"/api/v1/dimensions/{code}/values",
        json={"code": "BB", "name": "Bán buôn"},
        headers={**editor, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert added.status_code == 201, added.text
    assert added.json()["dimension_id"] == declared.json()["id"]

    listed = client.get(f"/api/v1/dimensions/{code}/values", headers=editor)
    assert [item["code"] for item in listed.json()["items"]] == ["BB"]


def test_replaying_a_dimension_declaration_returns_the_same_row(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Cùng lối idempotency với mọi `POST` tạo bản ghi khác.

    Ràng buộc `UNIQUE` chặn được bản ghi thứ hai, nhưng nó không cho người gửi
    lại biết lần trước đã thành công — họ nhận `409` và phải tự đoán.
    """
    key = uuid4().hex
    body = {"code": f"LAP{uuid4().hex[:6].upper()}", "name": "Chiều gửi lại"}

    first = client.post(
        "/api/v1/dimensions", json=body, headers={**editor, IDEMPOTENCY_HEADER: key}
    )
    second = client.post(
        "/api/v1/dimensions", json=body, headers={**editor, IDEMPOTENCY_HEADER: key}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_adding_the_same_value_code_to_two_dimensions_is_not_a_replay(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Vân tay của `POST .../values` phải mang **mã chiều**.

    Khóa idempotency có phạm vi theo route **khai báo** (`{code}` chưa điền), nên
    hai lời gọi thêm giá trị `BAC` cho hai chiều khác nhau dùng chung phạm vi.
    Không đưa `code` vào vân tay thì lời gọi thứ hai bị coi là gửi lại và nhận về
    giá trị của chiều thứ nhất — một giá trị thuộc chiều mà người gọi không hỏi.
    """
    key = uuid4().hex
    first_code = f"MOT{uuid4().hex[:6].upper()}"
    second_code = f"HAI{uuid4().hex[:6].upper()}"
    for code in (first_code, second_code):
        assert (
            _declare_dimension(client, editor, {"code": code, "name": f"Chiều {code}"}).status_code
            == 201
        )

    value_body = {"code": "BAC", "name": "Miền Bắc"}
    first = client.post(
        f"/api/v1/dimensions/{first_code}/values",
        json=value_body,
        headers={**editor, IDEMPOTENCY_HEADER: key},
    )
    second = client.post(
        f"/api/v1/dimensions/{second_code}/values",
        json=value_body,
        headers={**editor, IDEMPOTENCY_HEADER: key},
    )

    assert first.status_code == 201, first.text
    # Cùng khóa nhưng khác vân tay → `409 key_reused`, **không** phải một lượt
    # phát lại trả về giá trị của chiều kia.
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "idempotency.key_reused"


def test_declaring_a_dimension_without_an_idempotency_key_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/dimensions",
        json={"code": f"NOKEY{uuid4().hex[:6].upper()}", "name": "Không khóa"},
        headers=editor,
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_view_permission_does_not_grant_declaring_a_dimension(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Thêm một mã vật tư và khai một chiều phân tích mới là hai mức rủi ro khác nhau."""
    response = _declare_dimension(
        client, reader, {"code": f"CAM{uuid4().hex[:6].upper()}", "name": "Không được khai"}
    )
    assert response.status_code == 403


def test_an_unknown_dimension_code_is_reported_as_not_found(
    client: TestClient, editor: dict[str, str]
) -> None:
    response = client.get("/api/v1/dimensions/KHONG_CO_THAT", headers=editor)

    assert response.status_code == 404
    assert response.json()["error_code"] == "dimension.not_found"


# --------------------------------- cổng bổ sung sau review thù địch 3B-1


def _put(
    client: TestClient, headers: dict[str, str], slug: str, record: dict[str, object]
) -> object:
    """Gửi `PUT` hợp lệ cho một bản ghi — dùng để đo **ai** bị chặn, không phải luật sửa."""
    return client.put(
        f"/api/v1/master/{slug}/{record['id']}",
        json={
            "row_version": record["row_version"],
            "code": record["code"],
            "name": "Sửa từ chi nhánh khác",
            "name_en": None,
            "is_active": True,
        },
        headers=headers,
    )


def test_a_record_of_another_branch_cannot_be_updated(
    client: TestClient, editor: dict[str, str], both_branches_record: dict[str, object]
) -> None:
    """H-1: `PUT` lên bản ghi riêng của chi nhánh khác phải `404`.

    Ba đường **ghi** (`PUT`, `PUT .../parent`, `DELETE`) đều gọi `_ensure_visible`,
    nhưng trước review chỉ đường `GET` có cổng canh — gỡ cả ba dòng ấy thì toàn
    bộ 725 test vẫn xanh. Bảng danh mục cố ý không bật RLS (H39), nên ba dòng đó
    là thứ **duy nhất** chặn "chi nhánh A sửa danh mục riêng của chi nhánh B".
    """
    assert _put(client, editor, PLAIN, both_branches_record).status_code == 404


def test_a_record_of_another_branch_cannot_be_moved(
    client: TestClient, editor: dict[str, str], both_branches_record: dict[str, object]
) -> None:
    """H-1, đường thứ hai: chuyển nhánh."""
    response = client.put(
        f"/api/v1/master/{PLAIN}/{both_branches_record['id']}/parent",
        json={"row_version": both_branches_record["row_version"], "new_parent_id": None},
        headers=editor,
    )
    assert response.status_code == 404


def test_a_record_of_another_branch_cannot_be_deleted(
    client: TestClient, editor: dict[str, str], both_branches_record: dict[str, object]
) -> None:
    """H-1, đường thứ ba: xóa. Đây là đường mất dữ liệu, nên nó đáng một test riêng."""
    response = client.delete(f"/api/v1/master/{PLAIN}/{both_branches_record['id']}", headers=editor)
    assert response.status_code == 404


def test_a_subtree_anchor_of_another_branch_is_not_an_id_oracle(
    client: TestClient, editor: dict[str, str], both_branches_record: dict[str, object]
) -> None:
    """H-3: `?subtree_of=` một nút chi nhánh khác phải `404`, không phải `200 []`.

    Trước sửa: nút của chi nhánh khác → `200 {"items": []}`, id không tồn tại →
    `404`. Chênh lệch đó đủ để dò ra danh mục của chi nhánh bên cạnh gồm những
    id nào — một oracle liệt kê, dù không đọc được nội dung bản ghi nào.
    """
    foreign = client.get(
        f"/api/v1/master/{PLAIN}",
        params={"subtree_of": both_branches_record["id"]},
        headers=editor,
    )
    missing = client.get(
        f"/api/v1/master/{PLAIN}", params={"subtree_of": 99_999_999}, headers=editor
    )

    assert foreign.status_code == 404
    assert missing.status_code == 404


def test_a_parent_of_another_branch_is_not_an_id_oracle(
    client: TestClient, editor: dict[str, str], both_branches_record: dict[str, object]
) -> None:
    """H-4: nhóm cha ngoài phạm vi trả `404`, cùng mã với "không tồn tại".

    Trước sửa: cha của chi nhánh khác → `422` kèm `details.parent_branch_id`
    (id chi nhánh kia — dữ liệu người gọi chưa từng gõ vào), cha không tồn tại →
    `404`. Hai rò trong một.
    """
    foreign = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KCHA"), "name": "Con", "parent_id": both_branches_record["id"]},
    )
    missing = create_record(
        client,
        editor,
        PLAIN,
        {"code": unique_code("KCHB"), "name": "Con", "parent_id": 99_999_999},
    )

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert "parent_branch_id" not in foreign.json().get("details", {})


def test_creating_for_a_branch_other_than_the_acting_one_is_refused(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> None:
    """L-1: phạm vi **ghi** phải bằng phạm vi **đọc**.

    Người được gán cả A lẫn B, đang thao tác ở A, mà tạo được bản ghi
    `branch_id = B` thì mất dấu nó ngay: mọi đường đọc lọc theo
    `acting_branch_id`, nên bản ghi vừa tạo biến mất khỏi chính màn hình vừa tạo
    ra nó.
    """
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[catalog_branches[0]])}

    response = create_record(
        client,
        at_a,
        PLAIN,
        {
            "code": unique_code("KLECH"),
            "name": "Tạo cho chi nhánh không đang thao tác",
            "branch_id": ids[catalog_branches[1]],
        },
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"


def test_a_record_of_my_own_branch_is_listed(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> None:
    """M-6: mặt còn lại của cô lập chi nhánh — bản ghi **riêng của mình** phải hiện.

    Không có test này thì `_visible_to` trả về hằng `False` vẫn xanh: mọi khẳng
    định còn lại chỉ nói "không thấy bản ghi của người khác".
    """
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    mine = create_record(
        client,
        editor,
        PLAIN,
        {
            "code": unique_code("KMINE"),
            "name": "Kho riêng chi nhánh mình",
            "branch_id": ids[catalog_branches[0]],
        },
    )
    assert mine.status_code == 201, mine.text

    listed = client.get(f"/api/v1/master/{PLAIN}", params={"limit": 200}, headers=editor).json()
    assert mine.json()["id"] in {item["id"] for item in listed["items"]}


def test_a_record_with_extra_columns_is_born_with_one_audit_row(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    catalog_branches: list[str],
) -> None:
    """M-2: danh mục có cột riêng phải ra đời với `row_version = 1`, một dòng nhật ký.

    Trước sửa, router gọi `create` rồi `update` để đặt cột riêng → `row_version
    = 2` và nhật ký `['created', 'updated']` cho 4/17 danh mục. Đó đúng thứ mà
    `MasterDataService.create` và cơ chế `reserve_id` được dựng ra để tránh:
    *"một bản ghi mới toanh mang `row_version = 2` cùng một dòng nhật ký 'vừa
    tạo xong đã sửa' — hai thứ nhiễu mà kiểm toán viên phải tự loại"*.

    So sánh **cạnh nhau** với danh mục thuần cây: hai nửa của cùng một API không
    được có hai chất lượng nhật ký khác nhau.
    """
    plain = create_record(client, editor, PLAIN, {"code": unique_code("KNK"), "name": "Kho"}).json()
    with_extras = create_record(
        client,
        editor,
        WITH_EXTRAS,
        {"code": unique_code("DKNK"), "name": "Điều khoản", "due_days": 30},
    ).json()

    assert plain["row_version"] == 1
    assert with_extras["row_version"] == 1
    assert with_extras["due_days"] == 30

    # `audit_log` **có** bật RLS theo chi nhánh, nên phạm vi rỗng sẽ chỉ thấy
    # dòng `branch_id IS NULL` — tức là không thấy gì, và test sẽ xanh-rỗng theo
    # đúng kiểu nó sinh ra để bắt. Đọc bằng phạm vi của chính chi nhánh đã ghi.
    ids = branch_ids(session_factory, dataset_alpha, catalog_branches)
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(ids[catalog_branches[0]],),
    )
    with unit_of_work(session_factory, scope) as session:
        actions = session.execute(
            text(
                "SELECT action FROM audit_log WHERE entity_type = :entity_type "
                "AND entity_id = :entity_id ORDER BY id"
            ),
            {"entity_type": "payment_terms", "entity_id": str(with_extras["id"])},
        ).scalars()
        assert list(actions) == ["created"]


def test_the_same_idempotency_key_is_scoped_per_catalog(
    client: TestClient, editor: dict[str, str]
) -> None:
    """M-5: khóa idempotency có phạm vi theo **route**, và route mang `slug`.

    Cùng một khóa + cùng một thân request gửi tới hai danh mục khác nhau phải
    tạo ra **hai** bản ghi. Không có `slug` trong `route_key`, lời gọi thứ hai bị
    coi là gửi lại của lời gọi thứ nhất và trả về một bản ghi thuộc **danh mục
    khác** — đúng loại lỗi mà không màn hình nào phát hiện được.
    """
    key = uuid4().hex
    body = {"code": unique_code("KTRUNG"), "name": "Trùng khóa, khác danh mục"}

    first = create_record(client, editor, PLAIN, body, key=key)
    second = create_record(client, editor, SECOND_PLAIN, body, key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text


def test_a_different_body_under_the_same_key_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """M-5, mặt còn lại: vân tay phải **thật sự** đọc thân request.

    Vân tay hằng số cũng làm test trên xanh; chỉ khẳng định này giết được nó.
    """
    key = uuid4().hex
    first = create_record(
        client, editor, PLAIN, {"code": unique_code("KVT"), "name": "Bản đầu"}, key=key
    )
    second = create_record(
        client, editor, PLAIN, {"code": unique_code("KVT"), "name": "Thân khác"}, key=key
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert second.json()["error_code"] == "idempotency.key_reused"


# ------------------------------------------------------------- phân trang


def test_listing_is_paginated_with_a_total(client: TestClient, editor: dict[str, str]) -> None:
    """M-9: `limit`/`offset` + `total` — hợp đồng chốt **trước** khi 3B-3 cần nó.

    `total` là tổng trước khi cắt trang: màn hình cần nó để vẽ thanh cuộn và nói
    "1–2 trong 5", thứ không suy ra được từ độ dài trang.
    """
    root = create_record(
        client, editor, PLAIN, {"code": unique_code("KP"), "name": "Nhóm", "is_group": True}
    ).json()
    created = [
        create_record(
            client,
            editor,
            PLAIN,
            {"code": unique_code(f"KP{index}"), "name": f"Kho {index}", "parent_id": root["id"]},
        ).json()
        for index in range(5)
    ]

    page = client.get(
        f"/api/v1/master/{PLAIN}",
        params={"parent_id": root["id"], "limit": 2, "offset": 0},
        headers=editor,
    ).json()
    assert page["total"] == len(created)
    assert len(page["items"]) == 2

    rest = client.get(
        f"/api/v1/master/{PLAIN}",
        params={"parent_id": root["id"], "limit": 2, "offset": 4},
        headers=editor,
    ).json()
    assert rest["total"] == len(created)
    assert len(rest["items"]) == 1

    # Trang không được chồng lấn nhau — `ORDER BY` phải tất định.
    assert {item["id"] for item in page["items"]}.isdisjoint({item["id"] for item in rest["items"]})


def test_the_page_size_has_a_hard_ceiling(client: TestClient, editor: dict[str, str]) -> None:
    """Một `limit` khổng lồ không được kéo cả danh mục vật tư qua LAN."""
    response = client.get(
        f"/api/v1/master/{PLAIN}", params={"limit": MAX_PAGE_SIZE + 1}, headers=editor
    )
    assert response.status_code == 422
