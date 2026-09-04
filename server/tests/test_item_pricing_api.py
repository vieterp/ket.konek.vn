"""Ba bảng con giá và đường định giá qua HTTP (lát 7C-1).

`test_item_catalog_api.py` đã kiểm khuôn bảng con của vật tư hàng hóa; ở đây kiểm
bốn thứ **chỉ** lát này mới có:

* **`unit_id NULL` là một giá trị, không phải chỗ trống** — nó nghĩa "theo đơn vị
  chính", và gửi id của đơn vị chính lên tường minh thì bị **từ chối** chứ không
  âm thầm quy về `NULL`;
* **hai chỉ số duy nhất riêng phần** chặn đúng thứ một `UNIQUE` thường bỏ lọt:
  hai dòng "giá theo đơn vị chính" cùng một ô;
* **bậc chiết khấu cần đơn vị chính** để ngưỡng quy về (FR-SYS-045);
* **đường định giá** `POST /api/v1/pricing/quote` trả kết quả kèm **nguồn**.

Luật chọn giá có bộ test riêng đi thẳng qua `session` (`test_pricing_engine.py`);
ở đây chỉ khẳng định endpoint nối đúng dây và trả đúng hình dạng.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    catalog_codes,
    create_record,
    ensure_role,
    unique_code,
)
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.item import ItemNature
from ket.kernel.master_data.models.item_price_level import PriceDirection
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

ITEMS = "items"
UNITS = "units_of_measure"
PRICE_LISTS = "price_lists"
PARTNERS = "partners"
CONTRACTS = "contracts"

EDITOR_ROLE = "ke_toan_gia"


@pytest.fixture(scope="module")
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """`TestClient` dùng chung cho cả tệp, hạn mức tắt.

    **Không bài nào trong tệp này tạo chi nhánh** — người dùng dùng chung được gán
    chi nhánh một lần và không tự cập nhật; xem docstring dài ở
    `test_item_catalog_api.py` cho cả hai ràng buộc đi kèm `scope="module"`.
    """
    assert app_engine is not None and session_factory is not None
    unlimited = test_settings.model_copy(
        update={"rate_limit_per_minute": 0, "rate_limit_auth_per_minute": 0}
    )
    with api_test_client(create_app(unlimited)) as instance:
        yield instance


@pytest.fixture(scope="module")
def editor_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    every = (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE)
    return ensure_role(
        session_factory,
        dataset_alpha,
        EDITOR_ROLE,
        [
            *catalog_codes(ITEMS, *every),
            *catalog_codes(UNITS, *every),
            *catalog_codes(PRICE_LISTS, *every),
            *catalog_codes(PARTNERS, *every),
            *catalog_codes(CONTRACTS, *every),
        ],
    )


@pytest.fixture(scope="module")
def editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    test_password: str,
) -> dict[str, str]:
    """Kế toán giá, phạm vi mọi chi nhánh đang có."""
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        codes = list(session.scalars(select(Branch.code).order_by(Branch.code)).all())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "gia",
        test_password,
        branch_codes=codes,
    )


@pytest.fixture
def base_unit(client: TestClient, editor: dict[str, str]) -> int:
    return create_record(
        client, editor, UNITS, {"code": unique_code("DVC"), "name": "Chiếc"}
    ).json()["id"]


@pytest.fixture
def box_unit(client: TestClient, editor: dict[str, str]) -> int:
    return create_record(
        client, editor, UNITS, {"code": unique_code("DVT"), "name": "Thùng"}
    ).json()["id"]


@pytest.fixture
def item(client: TestClient, editor: dict[str, str], base_unit: int) -> int:
    response = create_record(
        client,
        editor,
        ITEMS,
        {
            "code": unique_code("VT"),
            "name": "Hàng có giá",
            "nature": ItemNature.GOODS.value,
            "base_unit_id": base_unit,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _post(client: TestClient, editor: dict[str, str], path: str, body: dict[str, object]) -> object:
    return client.post(path, json=body, headers={**editor, IDEMPOTENCY_HEADER: uuid4().hex})


def _prices(item_id: int) -> str:
    return f"/api/v1/master/items/{item_id}/prices"


def _tiers(item_id: int) -> str:
    return f"/api/v1/master/items/{item_id}/discount-tiers"


# ------------------------------------------------------------------ mức giá


def test_a_price_without_a_unit_means_the_base_unit(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Tầng cuối của thứ tự nguồn giá được khai bằng cách **để trống** ô đơn vị."""
    response = _post(
        client,
        editor,
        _prices(item),
        {"direction": PriceDirection.SALE.value, "level": 1, "price": "1000"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["unit_id"] is None

    listed = client.get(_prices(item), headers=editor).json()["items"]
    assert [row["price"] for row in listed] == ["1000.000000"]


def test_naming_the_base_unit_explicitly_is_refused(
    client: TestClient, editor: dict[str, str], item: int, base_unit: int
) -> None:
    """Hai cách viết cho cùng một dòng là hai dòng mà chỉ số duy nhất không thấy nhau.

    Im lặng quy về `NULL` cũng sai: người dùng sẽ không bao giờ biết mình đang
    khai sai chỗ.
    """
    response = _post(
        client,
        editor,
        _prices(item),
        {
            "unit_id": base_unit,
            "direction": PriceDirection.SALE.value,
            "level": 1,
            "price": "1000",
        },
    )
    assert response.status_code == 409
    assert response.json()["type"].endswith("item.price_unit_not_allowed")


def test_a_price_for_an_undeclared_conversion_unit_is_refused(
    client: TestClient, editor: dict[str, str], item: int, box_unit: int
) -> None:
    """Giá "theo từng đơn vị quy đổi" chỉ có nghĩa với đơn vị mã hàng **đã khai**."""
    response = _post(
        client,
        editor,
        _prices(item),
        {
            "unit_id": box_unit,
            "direction": PriceDirection.SALE.value,
            "level": 1,
            "price": "22000",
        },
    )
    assert response.status_code == 409
    assert response.json()["type"].endswith("item.price_unit_not_allowed")


def test_a_price_for_a_declared_conversion_unit_is_accepted(
    client: TestClient, editor: dict[str, str], item: int, box_unit: int
) -> None:
    assert (
        _post(
            client,
            editor,
            f"/api/v1/master/items/{item}/units",
            {"unit_id": box_unit, "factor": "24"},
        ).status_code
        == 201
    )
    response = _post(
        client,
        editor,
        _prices(item),
        {
            "unit_id": box_unit,
            "direction": PriceDirection.SALE.value,
            "level": 1,
            "price": "22000",
        },
    )
    assert response.status_code == 201, response.text


def test_two_base_unit_prices_in_one_slot_are_refused_by_the_partial_index(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Đây là ca mà một `UNIQUE` thường bỏ lọt: nó coi mọi `NULL` là khác nhau."""
    body = {"direction": PriceDirection.SALE.value, "level": 1, "price": "1000"}
    assert _post(client, editor, _prices(item), body).status_code == 201
    assert _post(client, editor, _prices(item), {**body, "price": "1100"}).status_code == 409


def test_the_same_slot_in_the_other_direction_is_a_different_row(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Chiều là một trục thật của khóa — giá mua và giá bán cùng mức 1 cùng tồn tại."""
    assert (
        _post(
            client,
            editor,
            _prices(item),
            {"direction": PriceDirection.SALE.value, "level": 1, "price": "1000"},
        ).status_code
        == 201
    )
    assert (
        _post(
            client,
            editor,
            _prices(item),
            {"direction": PriceDirection.PURCHASE.value, "level": 1, "price": "700"},
        ).status_code
        == 201
    )


def test_a_price_can_be_edited_and_deleted(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    created = _post(
        client,
        editor,
        _prices(item),
        {"direction": PriceDirection.SALE.value, "level": 1, "price": "1000"},
    ).json()

    updated = client.put(
        f"{_prices(item)}/{created['id']}",
        json={
            "row_version": created["row_version"],
            "direction": PriceDirection.SALE.value,
            "level": 2,
            "price": "1200",
            "label": "Bán buôn",
        },
        headers=editor,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["label"] == "Bán buôn"

    assert client.delete(f"{_prices(item)}/{created['id']}", headers=editor).status_code == 204
    assert client.get(_prices(item), headers=editor).json()["items"] == []


def test_a_price_of_another_item_is_not_reachable_through_this_item(
    client: TestClient, editor: dict[str, str], item: int, base_unit: int
) -> None:
    """Đường dẫn mang hai id; không đối chiếu chúng là một đường đọc vòng qua
    mọi phép kiểm đặt ở mã hàng chủ."""
    created = _post(
        client,
        editor,
        _prices(item),
        {"direction": PriceDirection.SALE.value, "level": 1, "price": "1000"},
    ).json()
    other = create_record(
        client,
        editor,
        ITEMS,
        {
            "code": unique_code("VT"),
            "name": "Hàng khác",
            "nature": ItemNature.GOODS.value,
            "base_unit_id": base_unit,
        },
    ).json()["id"]

    response = client.delete(f"{_prices(other)}/{created['id']}", headers=editor)
    assert response.status_code == 404


# ---------------------------------------------------------- bậc chiết khấu


def test_a_discount_tier_needs_a_base_unit_to_measure_against(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Dịch vụ khai không kèm đơn vị chính thì ngưỡng không quy về đâu được.

    Hệ quả đúng chứ không phải hạn chế kỹ thuật: chiết khấu theo số lượng cần một
    số lượng so được. Chiết khấu cho dịch vụ đi bằng mức giá hoặc gõ tay trên dòng.
    """
    service = create_record(
        client,
        editor,
        ITEMS,
        {"code": unique_code("DV"), "name": "Tư vấn", "nature": ItemNature.SERVICE.value},
    ).json()["id"]

    response = _post(
        client, editor, _tiers(service), {"min_quantity": "10", "discount_percent": "5"}
    )
    assert response.status_code == 409
    assert response.json()["type"].endswith("item.discount_tier_base_unit_missing")


def test_discount_tiers_are_listed_with_the_threshold_ascending(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Người dùng đọc bảng này như một thang; một thang đọc ngược phải đọc hai lần."""
    for threshold, percent in (("50", "7"), ("10", "2")):
        assert (
            _post(
                client,
                editor,
                _tiers(item),
                {"min_quantity": threshold, "discount_percent": percent},
            ).status_code
            == 201
        )

    listed = client.get(_tiers(item), headers=editor).json()["items"]
    assert [row["discount_percent"] for row in listed] == ["2.00", "7.00"]


def test_two_tiers_at_the_same_threshold_are_refused(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Luật "ngưỡng lớn nhất ≤ số lượng" không phân giải được cặp trùng ngưỡng."""
    body = {"min_quantity": "10", "discount_percent": "2"}
    assert _post(client, editor, _tiers(item), body).status_code == 201
    assert _post(client, editor, _tiers(item), {**body, "discount_percent": "3"}).status_code == 409


def test_a_threshold_of_zero_is_refused_by_the_request_body(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Ngưỡng 0 phủ mọi dòng nên nó không còn là một bậc — bảng giá mới là chỗ nói."""
    response = _post(client, editor, _tiers(item), {"min_quantity": "0", "discount_percent": "2"})
    assert response.status_code == 422


# ------------------------------------------------------------ dòng bảng giá


@pytest.fixture
def price_list(client: TestClient, editor: dict[str, str]) -> int:
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {
            "code": unique_code("BG"),
            "name": "Bảng giá bán lẻ",
            "direction": PriceDirection.SALE.value,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _lines(price_list_id: int) -> str:
    return f"/api/v1/master/price_lists/{price_list_id}/lines"


def test_a_price_list_line_defaults_to_every_quantity(
    client: TestClient, editor: dict[str, str], price_list: int, item: int
) -> None:
    """ "Bảng giá không phân theo số lượng" chỉ là mọi dòng để nguyên ngưỡng 1."""
    response = _post(client, editor, _lines(price_list), {"item_id": item, "price": "900"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["unit_id"] is None

    # So bằng `Decimal`, không bằng mặt chữ: thân trả về của đường **tạo** mang
    # giá trị mặc định của Python (`1`) còn đường **đọc** mang giá trị đã qua cột
    # `NUMERIC(20,6)` (`1.000000`). Cùng một con số, hai cách viết — bài này đo
    # ngưỡng mặc định, không đo cách tuần tự hóa.
    listed = client.get(_lines(price_list), headers=editor).json()["items"]
    assert [Decimal(row["min_quantity"]) for row in listed] == [Decimal(1)]


def test_two_lines_of_one_item_at_the_same_threshold_are_refused(
    client: TestClient, editor: dict[str, str], price_list: int, item: int
) -> None:
    body = {"item_id": item, "price": "900"}
    assert _post(client, editor, _lines(price_list), body).status_code == 201
    assert _post(client, editor, _lines(price_list), {**body, "price": "800"}).status_code == 409


def test_lines_at_different_thresholds_both_survive(
    client: TestClient, editor: dict[str, str], price_list: int, item: int
) -> None:
    assert (
        _post(client, editor, _lines(price_list), {"item_id": item, "price": "900"}).status_code
        == 201
    )
    assert (
        _post(
            client,
            editor,
            _lines(price_list),
            {"item_id": item, "min_quantity": "10", "price": "800"},
        ).status_code
        == 201
    )
    assert len(client.get(_lines(price_list), headers=editor).json()["items"]) == 2


def test_a_price_list_group_needs_no_direction(client: TestClient, editor: dict[str, str]) -> None:
    """Nút nhóm chỉ gom cây — bắt nó chọn chiều giá là bắt điền một ô vô nghĩa.

    Cùng khuôn `nature_set_unless_group` của vật tư hàng hóa: bộ định giá lọc
    `is_group = FALSE` nên chiều của một nhóm không đường nào đọc tới.
    """
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {"code": unique_code("NBG"), "name": "Bảng giá 2026", "is_group": True},
    )
    assert response.status_code == 201, response.text
    assert response.json()["direction"] is None


def test_a_price_list_group_may_not_carry_pricing_fields(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chiều ngược lại: nhóm bị **cấm**, không chỉ được miễn.

    Chỉ có vế "miễn" thì một nhóm khai được `direction`, và mọi truy vấn lọc theo
    chiều sẽ cộng cả nút nhóm — đúng lỗ hổng review H-1 của vật tư đã bắt.
    """
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {
            "code": unique_code("NBG"),
            "name": "Nhóm có chiều",
            "is_group": True,
            "direction": PriceDirection.SALE.value,
        },
    )
    assert response.status_code == 422


def test_a_leaf_price_list_still_needs_a_direction(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Bảng giá thật thì chiều là bắt buộc — đối tác vừa là khách vừa là NCC."""
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {"code": unique_code("BG"), "name": "Bảng giá thiếu chiều"},
    )
    assert response.status_code == 422


def test_an_effective_window_that_ends_before_it_starts_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Một bảng giá không ngày nào áp được đọc vẫn xuôi tai — đó là lý do có luật."""
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {
            "code": unique_code("BG"),
            "name": "Bảng giá lệch hạn",
            "direction": PriceDirection.SALE.value,
            "effective_from": "2026-12-31",
            "effective_to": "2026-01-01",
        },
    )
    assert response.status_code == 422


# ----------------------------------------------------------- đường định giá


def test_the_quote_endpoint_answers_with_the_source_that_replied(
    client: TestClient, editor: dict[str, str], price_list: int, item: int
) -> None:
    """Người dùng thấy một đơn giá tự điền; câu hỏi đầu tiên là "số này ở đâu ra"."""
    assert (
        _post(client, editor, _lines(price_list), {"item_id": item, "price": "900"}).status_code
        == 201
    )

    response = client.post(
        "/api/v1/pricing/quote",
        json={
            "item_id": item,
            "quantity": "1",
            "direction": PriceDirection.SALE.value,
            "on_date": "2026-06-15",
        },
        headers=editor,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "price_list"
    assert body["price_list_id"] == price_list
    assert Decimal(body["unit_price"]) == Decimal(900)


def test_the_quote_endpoint_needs_no_idempotency_key_because_it_writes_nothing(
    client: TestClient, editor: dict[str, str], item: int
) -> None:
    """Miễn trừ tường minh ở `IDEMPOTENCY_EXEMPT_PATHS` — nó không tạo gì cả."""
    response = client.post(
        "/api/v1/pricing/quote",
        json={
            "item_id": item,
            "quantity": "1",
            "direction": PriceDirection.SALE.value,
            "on_date": "2026-06-15",
        },
        headers=editor,
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "none"


def test_a_price_list_may_not_be_scoped_to_one_branch(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Bảng giá **luôn dùng chung toàn công ty** (user chốt 2026-09-04).

    Danh mục cố ý không bật RLS, nên lớp cô lập chi nhánh duy nhất là
    `MasterDataService._visible_to` ở đường đọc danh mục — mà bộ định giá đọc thẳng
    bảng ấy bằng một đường khác, không gọi nó. Đóng bằng **ràng buộc** thay vì thêm
    một phép lọc nữa: phép lọc là thứ đường đọc thứ ba sẽ lại quên (review H-1).
    """
    response = create_record(
        client,
        editor,
        PRICE_LISTS,
        {
            "code": unique_code("BG"),
            "name": "Bảng giá riêng chi nhánh",
            "direction": PriceDirection.SALE.value,
            "branch_id": 1,
        },
    )
    assert response.status_code == 422


def test_a_group_item_may_not_carry_the_tax_inclusive_flag_through_an_edit(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Nhóm được **cấm**, không chỉ được miễn — kể cả ở đường sửa (review M-1).

    Đường tạo đã có validator nói câu này từ lúc thêm cột; đường sửa thì không có
    `is_group` trong thân request nên nó phải đọc từ bản ghi, đúng lý do
    `UpdateGuard` tồn tại.
    """
    group = create_record(
        client, editor, ITEMS, {"code": unique_code("NVT"), "name": "Nhóm hàng", "is_group": True}
    ).json()

    response = client.put(
        f"/api/v1/master/items/{group['id']}",
        json={
            "row_version": group["row_version"],
            "code": group["code"],
            "name": group["name"],
            "is_active": True,
            "price_is_tax_inclusive": True,
        },
        headers=editor,
    )
    assert response.status_code == 422, response.text
    assert response.json()["type"].endswith("item.group_field_not_allowed")
    assert "giá sau thuế" in response.json()["detail"]
