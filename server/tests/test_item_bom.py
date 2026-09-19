"""Định mức NVL của mã hàng qua HTTP (FR-SYS-044, lát 8C-2).

Khuôn `test_item_catalog_api.py` (client + kế toán kho phạm vi toàn công ty dùng
chung cả tệp — cùng hai ràng buộc: không tạo chi nhánh trong bài, tắt hạn mức).
Kiểm những gì chỉ bảng con này mới có: linh kiện phải là hàng qua kho, không tự
trỏ, không tạo **vòng** qua nhiều cấp; `explode` nhân số lượng một cấp; gộp mã
hàng ba luật; xóa mã hàng đang là linh kiện bị `RESTRICT`.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    all_branch_codes,
    catalog_codes,
    create_record,
    ensure_branches,
    ensure_role,
    unique_code,
)
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.item import ItemNature
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

ITEMS = "items"
UNITS = "units_of_measure"
EDITOR_ROLE = "ke_toan_dinh_muc"
BRANCH_CODES = ["CN_BOM_A"]


@pytest.fixture(scope="module")
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
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
        [*catalog_codes(ITEMS, *every), *catalog_codes(UNITS, *every)],
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
    ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "dinhmuc",
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset_alpha),
    )


def _unit(client: TestClient, headers: dict[str, str]) -> int:
    response = create_record(
        client, headers, UNITS, {"code": unique_code("DVT"), "name": "Đơn vị thử"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _item(
    client: TestClient, headers: dict[str, str], *, base_unit_id: int | None, nature: str
) -> int:
    body: dict[str, object] = {"code": unique_code("VT"), "name": "Mặt hàng thử", "nature": nature}
    if base_unit_id is not None:
        body["base_unit_id"] = base_unit_id
    response = create_record(client, headers, ITEMS, body)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _goods(client: TestClient, headers: dict[str, str], base_unit_id: int) -> int:
    return _item(client, headers, base_unit_id=base_unit_id, nature=ItemNature.GOODS.value)


def _bom_url(item_id: int) -> str:
    return f"/api/v1/master/{ITEMS}/{item_id}/bom"


def _add(
    client: TestClient,
    headers: dict[str, str],
    item_id: int,
    component_id: int,
    *,
    quantity: str = "1",
    ratio: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {"component_item_id": component_id, "quantity": quantity}
    if ratio is not None:
        body["allocation_ratio"] = ratio
    return client.post(
        _bom_url(item_id), json=body, headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")}
    )


def _rows(
    client: TestClient, headers: dict[str, str], item_id: int
) -> dict[int, dict[str, object]]:
    response = client.get(_bom_url(item_id), headers=headers)
    assert response.status_code == 200, response.text
    return {int(row["component_item_id"]): dict(row) for row in response.json()["items"]}


def _merge(
    client: TestClient, headers: dict[str, str], *, source_id: int, target_id: int
) -> httpx.Response:
    return client.post(
        f"/api/v1/master/{ITEMS}/actions/merge",
        json={"source_id": source_id, "target_id": target_id},
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )


# --------------------------------------------------------------------- CRUD


def test_bom_lines_round_trip_and_explode_multiplies_one_level(
    client: TestClient, editor: dict[str, str]
) -> None:
    piece, kg = _unit(client, editor), _unit(client, editor)
    product = _goods(client, editor, piece)
    screw, plate = _goods(client, editor, piece), _goods(client, editor, kg)
    assert _add(client, editor, product, screw, quantity="4").status_code == 201
    created = _add(client, editor, product, plate, quantity="0.5", ratio="3")
    assert created.status_code == 201, created.text
    assert Decimal(created.json()["allocation_ratio"]) == Decimal(3)

    rows = _rows(client, editor, product)
    assert Decimal(rows[screw]["quantity"]) == Decimal(4)
    assert Decimal(rows[screw]["allocation_ratio"]) == Decimal(1)  # mặc định

    exploded = client.get(_bom_url(product) + "/explode", params={"quantity": "3"}, headers=editor)
    assert exploded.status_code == 200, exploded.text
    lines = {int(line["component_item_id"]): line for line in exploded.json()["lines"]}
    assert Decimal(lines[screw]["quantity"]) == Decimal(12)
    assert lines[screw]["unit_id"] == piece
    assert Decimal(lines[plate]["quantity"]) == Decimal("1.5")
    assert lines[plate]["unit_id"] == kg
    assert Decimal(lines[plate]["allocation_ratio"]) == Decimal(3)

    # Sửa trọn giá trị có kiểm phiên bản; xóa rồi danh sách rỗng dòng ấy.
    row = rows[screw]
    updated = client.put(
        f"{_bom_url(product)}/{row['id']}",
        json={
            "component_item_id": screw,
            "quantity": "5",
            "allocation_ratio": "2",
            "row_version": row["row_version"],
        },
        headers=editor,
    )
    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["quantity"]) == Decimal(5)
    stale = client.put(
        f"{_bom_url(product)}/{row['id']}",
        json={
            "component_item_id": screw,
            "quantity": "6",
            "allocation_ratio": "2",
            "row_version": row["row_version"],
        },
        headers=editor,
    )
    assert stale.status_code == 409, stale.text
    assert client.delete(f"{_bom_url(product)}/{row['id']}", headers=editor).status_code == 204
    assert screw not in _rows(client, editor, product)


def test_the_same_component_twice_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    piece = _unit(client, editor)
    product, screw = _goods(client, editor, piece), _goods(client, editor, piece)
    assert _add(client, editor, product, screw).status_code == 201
    response = _add(client, editor, product, screw, quantity="2")
    assert response.status_code == 409, response.text


def test_component_must_be_a_stocked_item_and_not_the_product_itself(
    client: TestClient, editor: dict[str, str]
) -> None:
    piece = _unit(client, editor)
    product = _goods(client, editor, piece)
    service = _item(client, editor, base_unit_id=None, nature=ItemNature.SERVICE.value)

    own = _add(client, editor, product, product)
    assert own.status_code == 422, own.text
    assert own.json()["error_code"] == "item.bom_component_invalid"

    not_stocked = _add(client, editor, product, service)
    assert not_stocked.status_code == 422, not_stocked.text
    assert not_stocked.json()["error_code"] == "item.bom_component_invalid"


def test_a_cycle_through_two_levels_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """A cần B, B cần C — thêm "C cần A" là vòng ba cấp; engine sẽ không hội tụ."""
    piece = _unit(client, editor)
    a, b, c = (_goods(client, editor, piece) for _ in range(3))
    assert _add(client, editor, a, b).status_code == 201
    assert _add(client, editor, b, c).status_code == 201

    response = _add(client, editor, c, a)

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "item.bom_cycle"
    # Sửa một dòng sang linh kiện tạo vòng cũng bị chặn.
    row = _rows(client, editor, b)[c]
    turned = client.put(
        f"{_bom_url(b)}/{row['id']}",
        json={
            "component_item_id": a,
            "quantity": "1",
            "allocation_ratio": "1",
            "row_version": row["row_version"],
        },
        headers=editor,
    )
    assert turned.status_code == 422, turned.text
    assert turned.json()["error_code"] == "item.bom_cycle"


def test_deleting_an_item_used_as_a_component_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    piece = _unit(client, editor)
    product, screw = _goods(client, editor, piece), _goods(client, editor, piece)
    assert _add(client, editor, product, screw).status_code == 201

    response = client.delete(f"/api/v1/master/{ITEMS}/{screw}", headers=editor)

    # `RESTRICT` của DB chặn — cùng đường với xóa đơn vị tính còn `item_units`
    # trỏ tới (bảng con không ghi bộ đếm sử dụng); handler dịch FK thành 422.
    assert response.status_code == 422, response.text
    assert response.json()["details"]["constraint"] == "fk_item_bom_lines_component_item_id"
    # Xóa thành phẩm thì định mức đi theo (CASCADE) — không lỗi.
    assert client.delete(f"/api/v1/master/{ITEMS}/{product}", headers=editor).status_code == 204


# ---------------------------------------------------------------------- gộp


def test_merging_items_folds_components_declared_on_both_sides(
    client: TestClient, editor: dict[str, str]
) -> None:
    piece = _unit(client, editor)
    screw, bolt, nut = (_goods(client, editor, piece) for _ in range(3))
    source, target = _goods(client, editor, piece), _goods(client, editor, piece)
    assert _add(client, editor, source, screw, quantity="4").status_code == 201
    assert _add(client, editor, source, bolt, quantity="2").status_code == 201
    assert _add(client, editor, target, screw, quantity="6").status_code == 201
    # Cả nguồn lẫn đích đều là linh kiện của `nut`.
    assert _add(client, editor, nut, source, quantity="1").status_code == 201
    assert _add(client, editor, nut, target, quantity="3").status_code == 201

    response = _merge(client, editor, source_id=source, target_id=target)

    assert response.status_code == 200, response.text
    rows = _rows(client, editor, target)
    assert {k: Decimal(str(v["quantity"])) for k, v in rows.items()} == {
        screw: Decimal(6),  # bản đích thắng
        bolt: Decimal(2),  # chỉ nguồn có → đi theo
    }
    assert {k: Decimal(str(v["quantity"])) for k, v in _rows(client, editor, nut).items()} == {
        target: Decimal(3)
    }


def test_merging_an_item_into_its_own_component_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    piece = _unit(client, editor)
    product, screw = _goods(client, editor, piece), _goods(client, editor, piece)
    assert _add(client, editor, product, screw).status_code == 201

    response = _merge(client, editor, source_id=screw, target_id=product)

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "bom_self_reference"


def test_merging_that_would_close_a_cycle_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Nguồn là linh kiện của B, B là linh kiện của đích → gộp xong đích cần B cần đích."""
    piece = _unit(client, editor)
    source, b, target = (_goods(client, editor, piece) for _ in range(3))
    assert _add(client, editor, b, source).status_code == 201
    assert _add(client, editor, target, b).status_code == 201

    response = _merge(client, editor, source_id=source, target_id=target)

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "item.bom_cycle"
    # Không gì đổi: transaction hoàn lại.
    assert set(_rows(client, editor, b)) == {source}
