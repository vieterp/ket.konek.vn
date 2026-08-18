"""Vật tư hàng hóa và hai bảng con của nó qua HTTP (lát 3B-3).

`test_master_data_api.py` đã kiểm bộ sinh route, `test_partner_catalog_api.py` đã
kiểm khuôn bảng con. Ở đây kiểm bốn thứ **chỉ** lát này mới có:

* **tính chất** quyết định cái gì được khai (FR-SYS-040): đơn vị chính bắt buộc
  cho thứ đi qua kho, kho ngầm định vô nghĩa với dịch vụ, quy cách chỉ dành cho
  hàng hóa và thành phẩm;
* **trường chốt một lần** (H69): `nature` và `base_unit_id` không có trong thân
  request sửa, và đường sửa **không** được âm thầm xóa trắng chúng;
* **đơn vị quy đổi** phẳng về đơn vị chính (FR-SYS-041), gồm cả hai phép kiểm mà
  DB không diễn đạt được (không trùng đơn vị chính, mã hàng phải có đơn vị chính);
* **gộp bản ghi** ở cả hai chiều của `uq_item_units_item_unit`: gộp hai mã hàng
  (H71 — khác đơn vị chính thì từ chối) và gộp hai **đơn vị tính**.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    all_branch_codes,
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
from ket.kernel.master_data.models.item import ItemNature
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.master_data.models.item_variant import ItemVariant
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

ITEMS = "items"
UNITS = "units_of_measure"
WAREHOUSES = "warehouses"

EDITOR_ROLE = "ke_toan_vat_tu"
BRANCH_CODES = ["CN_VT_A", "CN_VT_B"]


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
            *catalog_codes(ITEMS, *every),
            *catalog_codes(UNITS, *every),
            *catalog_codes(WAREHOUSES, *every),
        ],
    )


@pytest.fixture(scope="module")
def item_branches(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> list[str]:
    return ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)


@pytest.fixture
def editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    item_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Kế toán kho có **phạm vi toàn công ty**.

    Phạm vi đầy đủ vì cùng tệp này gộp bản ghi, và gộp đòi đúng điều đó
    (`_ensure_company_wide_scope`, H63). Test về phạm vi hẹp đã có ở
    `test_master_data_merge.py`; ở đây phạm vi không phải thứ đang đo.
    """
    assert item_branches, "cần ít nhất một chi nhánh"
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "vattu",
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset_alpha),
    )


# --------------------------------------------------------------------------- khung


def _unit(client: TestClient, headers: dict[str, str], *, is_group: bool = False) -> int:
    response = create_record(
        client,
        headers,
        UNITS,
        {"code": unique_code("DVT"), "name": "Đơn vị thử", "is_group": is_group},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _warehouse(client: TestClient, headers: dict[str, str]) -> int:
    response = create_record(
        client, headers, WAREHOUSES, {"code": unique_code("KHO"), "name": "Kho thử"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _create_item(client: TestClient, headers: dict[str, str], **extra: object) -> httpx.Response:
    body: dict[str, object] = {
        "code": unique_code("VT"),
        "name": "Mặt hàng thử",
        **extra,
    }
    return create_record(client, headers, ITEMS, body)


def _goods(
    client: TestClient, headers: dict[str, str], base_unit_id: int, **extra: object
) -> dict[str, object]:
    response = _create_item(
        client,
        headers,
        nature=ItemNature.GOODS.value,
        base_unit_id=base_unit_id,
        **extra,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _units_url(item_id: int) -> str:
    return f"/api/v1/master/{ITEMS}/{item_id}/units"


def _variants_url(item_id: int) -> str:
    return f"/api/v1/master/{ITEMS}/{item_id}/variants"


def _add_unit(
    client: TestClient,
    headers: dict[str, str],
    item_id: int,
    unit_id: int,
    *,
    factor: str,
) -> httpx.Response:
    return client.post(
        _units_url(item_id),
        json={"unit_id": unit_id, "factor": factor},
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )


def _add_variant(
    client: TestClient, headers: dict[str, str], item_id: int, *, code: str, name: str = "Quy cách"
) -> httpx.Response:
    return client.post(
        _variants_url(item_id),
        json={"code": code, "name": name},
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )


def _merge(
    client: TestClient, headers: dict[str, str], slug: str, *, source_id: int, target_id: int
) -> httpx.Response:
    return client.post(
        f"/api/v1/master/{slug}/actions/merge",
        json={"source_id": source_id, "target_id": target_id},
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )


# --------------------------------------------------- tính chất quyết định hành vi


def test_an_item_without_a_nature_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """FR-SYS-040: tính chất quyết định hành vi tồn kho và hạch toán, nên nó bắt buộc."""
    response = _create_item(client, editor)

    assert response.status_code == 422, response.text
    assert "tính chất" in response.text


def test_an_item_group_needs_no_nature(client: TestClient, editor: dict[str, str]) -> None:
    """Đối trọng: nút nhóm chỉ gom cây, không bao giờ lên chứng từ."""
    response = _create_item(client, editor, is_group=True)

    assert response.status_code == 201, response.text
    assert response.json()["nature"] is None


def test_goods_without_a_base_unit_are_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Số tồn của hàng hóa lưu theo đơn vị chính, nên thiếu nó là cột tồn không có đơn vị đo."""
    response = _create_item(client, editor, nature=ItemNature.GOODS.value)

    assert response.status_code == 422, response.text
    assert "đơn vị tính chính" in response.text


def test_a_service_needs_no_base_unit(client: TestClient, editor: dict[str, str]) -> None:
    """Đối trọng: dịch vụ không đi qua kho nên không cần đơn vị chính."""
    response = _create_item(client, editor, nature=ItemNature.SERVICE.value)

    assert response.status_code == 201, response.text


def test_a_service_cannot_carry_a_default_warehouse(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Kho ngầm định của một dịch vụ là giá trị không đường nào đọc tới."""
    response = _create_item(
        client,
        editor,
        nature=ItemNature.SERVICE.value,
        warehouse_id=_warehouse(client, editor),
    )

    assert response.status_code == 422, response.text
    assert "kho ngầm định" in response.text


def test_a_base_unit_that_is_a_group_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """`CatalogSpec.references`: nút nhóm tồn tại nhưng không mang giá trị nào."""
    response = _create_item(
        client,
        editor,
        nature=ItemNature.GOODS.value,
        base_unit_id=_unit(client, editor, is_group=True),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "master_data.group_not_postable"


# ------------------------------------------------- trường chốt một lần lúc tạo (H69)


def test_the_update_body_refuses_the_create_only_fields(
    client: TestClient, editor: dict[str, str]
) -> None:
    """`nature` và `base_unit_id` không có trong thân request sửa.

    `extra="forbid"` biến lần gửi sai thành một thông điệp thay vì một giá trị bị
    bỏ qua im lặng — người dùng thấy form lưu xong mà giá trị vừa nhập không đổi
    là kiểu lỗi tốn nhiều giờ hỗ trợ nhất.
    """
    item = _goods(client, editor, _unit(client, editor))

    response = client.put(
        f"/api/v1/master/{ITEMS}/{item['id']}",
        json={
            "row_version": item["row_version"],
            "code": str(item["code"]),
            "name": "Tên mới",
            "name_en": None,
            "is_active": True,
            "warehouse_id": None,
            "description": None,
            "nature": ItemNature.SERVICE.value,
        },
        headers=editor,
    )

    assert response.status_code == 422, response.text


def test_updating_an_item_keeps_its_nature_and_base_unit(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Sửa tên **không** được xóa trắng hai trường chốt một lần.

    Đây là đối trọng của test trên và là chỗ dễ hỏng nhất của cơ chế: nếu
    `extra_values` lấy `getattr(payload, field, None)` thay vì giao với tập trường
    thật có, thì mỗi lần sửa tên sẽ ghi `nature = NULL` và `base_unit_id = NULL` —
    hai cột biến mất mà không request nào nhắc tới chúng.
    """
    base_unit = _unit(client, editor)
    item = _goods(client, editor, base_unit, description="Mô tả gốc")

    response = client.put(
        f"/api/v1/master/{ITEMS}/{item['id']}",
        json={
            "row_version": item["row_version"],
            "code": str(item["code"]),
            "name": "Tên mới",
            "name_en": None,
            "is_active": True,
            "warehouse_id": None,
            "description": "Mô tả mới",
        },
        headers=editor,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Tên mới"
    assert body["description"] == "Mô tả mới"
    assert body["nature"] == ItemNature.GOODS.value
    assert body["base_unit_id"] == base_unit


# ------------------------------------------------------------- đơn vị quy đổi


def test_conversion_units_are_listed_largest_factor_first(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Thứ tự theo độ lớn là thứ tự người dùng nghĩ về bảng này (thùng rồi lố)."""
    item = _goods(client, editor, _unit(client, editor))
    box = _unit(client, editor)
    dozen = _unit(client, editor)

    assert _add_unit(client, editor, int(item["id"]), dozen, factor="12").status_code == 201
    assert _add_unit(client, editor, int(item["id"]), box, factor="24").status_code == 201

    listed = client.get(_units_url(int(item["id"])), headers=editor)

    assert listed.status_code == 200, listed.text
    factors = [Decimal(row["factor"]) for row in listed.json()["items"]]
    assert factors == [Decimal(24), Decimal(12)]


def test_a_conversion_unit_equal_to_the_base_unit_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đơn vị chính luôn có tỷ lệ 1 và không nằm trong bảng này.

    Không có phép kiểm này thì một mã hàng có hai tỷ lệ cho cùng một đơn vị — một
    cái ngầm định bằng 1, một cái người dùng khai — và `CHECK` của DB không diễn
    đạt được điều đó vì nó so hai bảng.
    """
    base_unit = _unit(client, editor)
    item = _goods(client, editor, base_unit)

    response = _add_unit(client, editor, int(item["id"]), base_unit, factor="24")

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "item.unit_duplicates_base"


def test_a_service_cannot_declare_conversion_units(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chưa có đơn vị chính thì tỷ lệ quy đổi không quy về đâu cả."""
    created = _create_item(client, editor, nature=ItemNature.SERVICE.value)
    assert created.status_code == 201, created.text

    response = _add_unit(
        client, editor, int(created.json()["id"]), _unit(client, editor), factor="2"
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "item.base_unit_missing"


def test_the_same_unit_twice_on_one_item_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Hai tỷ lệ cho cùng phép quy đổi — không truy vấn nào chọn được cái đúng."""
    item = _goods(client, editor, _unit(client, editor))
    box = _unit(client, editor)

    assert _add_unit(client, editor, int(item["id"]), box, factor="24").status_code == 201
    again = _add_unit(client, editor, int(item["id"]), box, factor="30")

    assert again.status_code == 409, again.text


@pytest.mark.parametrize("factor", ["0", "-1"])
def test_a_non_positive_factor_is_refused(
    client: TestClient, editor: dict[str, str], factor: str
) -> None:
    """Tỷ lệ 0 biến mọi số lượng thành 0; tỷ lệ âm biến nhập thành xuất."""
    item = _goods(client, editor, _unit(client, editor))

    response = _add_unit(client, editor, int(item["id"]), _unit(client, editor), factor=factor)

    assert response.status_code == 422, response.text


def test_a_conversion_unit_of_another_item_is_out_of_reach(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đường dẫn mang cả hai id; không đối chiếu chúng là một đường ghi vòng qua
    mọi phép kiểm đặt ở mã hàng chủ."""
    base_unit = _unit(client, editor)
    mine = _goods(client, editor, base_unit)
    other = _goods(client, editor, base_unit)
    row = _add_unit(client, editor, int(other["id"]), _unit(client, editor), factor="6")
    assert row.status_code == 201, row.text
    row_id = int(row.json()["id"])

    updated = client.put(
        f"{_units_url(int(mine['id']))}/{row_id}",
        json={
            "row_version": row.json()["row_version"],
            "unit_id": _unit(client, editor),
            "factor": "3",
        },
        headers=editor,
    )
    deleted = client.delete(f"{_units_url(int(mine['id']))}/{row_id}", headers=editor)

    assert updated.status_code == 404, updated.text
    assert deleted.status_code == 404, deleted.text


def test_a_stale_row_version_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Khóa lạc quan (FR-NFR-005) áp cho cả bảng con."""
    item = _goods(client, editor, _unit(client, editor))
    box = _unit(client, editor)
    row = _add_unit(client, editor, int(item["id"]), box, factor="24")
    assert row.status_code == 201, row.text

    response = client.put(
        f"{_units_url(int(item['id']))}/{row.json()['id']}",
        json={"row_version": 99, "unit_id": box, "factor": "30"},
        headers=editor,
    )

    assert response.status_code == 409, response.text


def test_resending_the_same_key_adds_one_conversion_unit(
    client: TestClient, editor: dict[str, str]
) -> None:
    """FR-NFR-004: gửi lại sau khi mạng rớt không được thêm dòng thứ hai."""
    item = _goods(client, editor, _unit(client, editor))
    box = _unit(client, editor)
    key = unique_code("KEY")
    payload = {"unit_id": box, "factor": "24"}

    first = client.post(
        _units_url(int(item["id"])), json=payload, headers={**editor, IDEMPOTENCY_HEADER: key}
    )
    second = client.post(
        _units_url(int(item["id"])), json=payload, headers={**editor, IDEMPOTENCY_HEADER: key}
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    listed = client.get(_units_url(int(item["id"])), headers=editor)
    assert len(listed.json()["items"]) == 1


def test_a_group_item_has_no_child_rows(client: TestClient, editor: dict[str, str]) -> None:
    """Nhóm không lên chứng từ nên dữ liệu chi tiết của nó không đường nào đọc tới."""
    created = _create_item(client, editor, is_group=True)
    assert created.status_code == 201, created.text

    response = client.get(_units_url(int(created.json()["id"])), headers=editor)

    assert response.status_code == 404, response.text


def test_deleting_a_conversion_unit_removes_it(client: TestClient, editor: dict[str, str]) -> None:
    item = _goods(client, editor, _unit(client, editor))
    row = _add_unit(client, editor, int(item["id"]), _unit(client, editor), factor="24")
    assert row.status_code == 201, row.text

    deleted = client.delete(f"{_units_url(int(item['id']))}/{row.json()['id']}", headers=editor)

    assert deleted.status_code == 204, deleted.text
    assert client.get(_units_url(int(item["id"])), headers=editor).json()["items"] == []


# ------------------------------------------------------------------ mã quy cách


def test_variants_hide_the_inactive_ones_by_default(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đây là nguồn dựng ô chọn trên chứng từ; quy cách đã ngừng hiện ở đó thì việc
    ngừng nó chẳng có tác dụng gì."""
    item = _goods(client, editor, _unit(client, editor))
    live = _add_variant(client, editor, int(item["id"]), code="DO")
    stopped = _add_variant(client, editor, int(item["id"]), code="XANH")
    assert live.status_code == 201 and stopped.status_code == 201, stopped.text
    closed = client.put(
        f"{_variants_url(int(item['id']))}/{stopped.json()['id']}",
        json={
            "row_version": stopped.json()["row_version"],
            "code": "XANH",
            "name": "Xanh",
            "is_active": False,
        },
        headers=editor,
    )
    assert closed.status_code == 200, closed.text

    default_view = client.get(_variants_url(int(item["id"])), headers=editor)
    full_view = client.get(
        _variants_url(int(item["id"])), headers=editor, params={"include_inactive": True}
    )

    assert [row["code"] for row in default_view.json()["items"]] == ["DO"]
    assert [row["code"] for row in full_view.json()["items"]] == ["DO", "XANH"]


def test_a_service_cannot_declare_variants(client: TestClient, editor: dict[str, str]) -> None:
    """Quy cách là một trục của báo cáo tồn kho, nên nó chỉ có nghĩa với thứ có tồn."""
    created = _create_item(client, editor, nature=ItemNature.SERVICE.value)
    assert created.status_code == 201, created.text

    response = _add_variant(client, editor, int(created.json()["id"]), code="DO")

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "item.variant_not_supported"


def test_a_variant_code_is_unique_inside_one_item_only(
    client: TestClient, editor: dict[str, str]
) -> None:
    """ "Đỏ" của áo và "Đỏ" của mũ là hai dòng độc lập; hai lần "Đỏ" của áo thì không."""
    base_unit = _unit(client, editor)
    shirt = _goods(client, editor, base_unit)
    hat = _goods(client, editor, base_unit)

    assert _add_variant(client, editor, int(shirt["id"]), code="DO").status_code == 201
    assert _add_variant(client, editor, int(hat["id"]), code="DO").status_code == 201
    again = _add_variant(client, editor, int(shirt["id"]), code="DO")

    assert again.status_code == 409, again.text


def test_a_variant_of_another_item_is_out_of_reach(
    client: TestClient, editor: dict[str, str]
) -> None:
    base_unit = _unit(client, editor)
    mine = _goods(client, editor, base_unit)
    other = _goods(client, editor, base_unit)
    variant = _add_variant(client, editor, int(other["id"]), code="DO")
    assert variant.status_code == 201, variant.text

    response = client.delete(
        f"{_variants_url(int(mine['id']))}/{variant.json()['id']}", headers=editor
    )

    assert response.status_code == 404, response.text


def test_deleting_an_item_takes_its_children_along(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """`ON DELETE CASCADE`: bảng con **thuộc về** mã hàng, không tồn tại độc lập."""
    item = _goods(client, editor, _unit(client, editor))
    item_id = int(item["id"])
    assert _add_unit(client, editor, item_id, _unit(client, editor), factor="24").status_code == 201
    assert _add_variant(client, editor, item_id, code="DO").status_code == 201

    deleted = client.delete(f"/api/v1/master/{ITEMS}/{item_id}", headers=editor)

    assert deleted.status_code == 204, deleted.text
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        units = session.scalars(select(ItemUnit.id).where(ItemUnit.item_id == item_id)).all()
        variants = session.scalars(
            select(ItemVariant.id).where(ItemVariant.item_id == item_id)
        ).all()
    assert units == []
    assert variants == []


# ------------------------------------------------------------------ gộp bản ghi


def test_merging_items_with_different_base_units_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """H71: số tồn của hai mã hàng khác đơn vị chính không cộng được với nhau.

    Từ chối là câu trả lời đúng, không phải một hạn chế kỹ thuật: chuyển tỷ lệ quy
    đổi sang một đơn vị chính khác là giữ nguyên con số và đổi nghĩa của nó.
    """
    source = _goods(client, editor, _unit(client, editor))
    target = _goods(client, editor, _unit(client, editor))

    response = _merge(
        client, editor, ITEMS, source_id=int(source["id"]), target_id=int(target["id"])
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "master_data.merge_refused"
    assert body["details"]["reason"] == "base_unit_differs"


def test_merging_items_folds_a_conversion_unit_declared_on_both_sides(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Ca thường gặp nhất của FR-SYS-016: hai bản trùng thì cả hai đã khai "thùng".

    Không có hook thì câu `UPDATE` chuyển khóa ngoại đụng `uq_item_units_item_unit`
    và người dùng nhận tên một chỉ mục nội bộ (khuôn H1 của lát 3B-2).
    """
    base_unit = _unit(client, editor)
    box = _unit(client, editor)
    dozen = _unit(client, editor)
    source = _goods(client, editor, base_unit)
    target = _goods(client, editor, base_unit)
    assert _add_unit(client, editor, int(source["id"]), box, factor="24").status_code == 201
    assert _add_unit(client, editor, int(source["id"]), dozen, factor="12").status_code == 201
    assert _add_unit(client, editor, int(target["id"]), box, factor="30").status_code == 201

    response = _merge(
        client, editor, ITEMS, source_id=int(source["id"]), target_id=int(target["id"])
    )

    assert response.status_code == 200, response.text
    rows = client.get(_units_url(int(target["id"])), headers=editor).json()["items"]
    by_unit = {row["unit_id"]: Decimal(row["factor"]) for row in rows}
    # Tỷ lệ của bản **giữ lại** thắng; đơn vị chỉ nguồn có thì đi theo.
    assert by_unit == {box: Decimal(30), dozen: Decimal(12)}


def test_merging_items_folds_a_variant_declared_on_both_sides(
    client: TestClient, editor: dict[str, str]
) -> None:
    base_unit = _unit(client, editor)
    source = _goods(client, editor, base_unit)
    target = _goods(client, editor, base_unit)
    assert _add_variant(client, editor, int(source["id"]), code="DO").status_code == 201
    assert _add_variant(client, editor, int(source["id"]), code="XANH").status_code == 201
    assert _add_variant(client, editor, int(target["id"]), code="DO").status_code == 201

    response = _merge(
        client, editor, ITEMS, source_id=int(source["id"]), target_id=int(target["id"])
    )

    assert response.status_code == 200, response.text
    codes = [
        row["code"]
        for row in client.get(_variants_url(int(target["id"])), headers=editor).json()["items"]
    ]
    assert sorted(codes) == ["DO", "XANH"]


def test_merging_two_units_of_measure_folds_the_rows_of_one_item(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Chiều còn lại của `uq_item_units_item_unit` — vì sao `units_of_measure` cũng
    phải khai hook (H70).

    Hai đơn vị trùng nghĩa ("cái" và "chiếc") thường bị khai lẫn lộn **trên cùng
    một mã hàng**, nên gộp chúng đụng ràng buộc duy nhất ở đúng ca phổ biến.

    **Cùng** tỷ lệ ở hai dòng, có chủ đích: hai tỷ lệ khác nhau là bằng chứng hai
    đơn vị không phải một, và lần gộp ấy nay bị từ chối
    (`test_merging_units_declared_with_different_factors_on_one_item_is_refused`).
    Bản đầu tiên của test này dùng 24 và 30 rồi khẳng định "30 thắng" — tức nó
    **hợp thức hóa** đúng lỗi C1 mà review tìm ra.
    """
    base_unit = _unit(client, editor)
    source_unit = _unit(client, editor)
    target_unit = _unit(client, editor)
    item = _goods(client, editor, base_unit)
    assert _add_unit(client, editor, int(item["id"]), source_unit, factor="24").status_code == 201
    assert _add_unit(client, editor, int(item["id"]), target_unit, factor="24").status_code == 201

    response = _merge(client, editor, UNITS, source_id=source_unit, target_id=target_unit)

    assert response.status_code == 200, response.text
    rows = client.get(_units_url(int(item["id"])), headers=editor).json()["items"]
    assert [(row["unit_id"], Decimal(row["factor"])) for row in rows] == [
        (target_unit, Decimal(24))
    ]


# ------------------------------------- nhóm không mang dữ liệu của mã hàng thật (H-1)


@pytest.mark.parametrize(
    ("field", "value_kind"),
    [("nature", "nature"), ("base_unit_id", "unit"), ("warehouse_id", "warehouse")],
)
def test_a_group_cannot_carry_item_data(
    client: TestClient, editor: dict[str, str], field: str, value_kind: str
) -> None:
    """Nhóm bị **cấm** mang tính chất/đơn vị chính/kho, không chỉ được **miễn**.

    Bản đầu tiên của lát này chỉ có `CHECK (is_group OR nature IS NOT NULL)`, tức
    một nhóm khai được `nature = 'goods'` (review H-1). Hậu quả nằm ở phase 8:
    mọi truy vấn tồn kho lọc theo `nature IN ('goods','finished_goods')` sẽ cộng
    cả nút nhóm vào báo cáo, và không lần review nào ở đó nghi ngờ danh mục.
    """
    value: object = ItemNature.GOODS.value
    if value_kind == "unit":
        value = _unit(client, editor)
    elif value_kind == "warehouse":
        value = _warehouse(client, editor)

    response = _create_item(client, editor, is_group=True, **{field: value})

    assert response.status_code == 422, response.text
    assert "Nhóm vật tư" in response.text


# ------------------------------------------------- luật liên-trường ở đường SỬA (H-4)


def test_updating_a_service_with_a_warehouse_is_refused_in_vietnamese(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đường **sửa** phải trả cùng câu tiếng Việt như đường tạo, không phải tên ràng buộc.

    Luật này cần `nature`, mà `nature` chốt một lần nên nó vắng mặt ở thân request
    sửa — validator của `ItemFields` vì thế chỉ chạy ở đường tạo, và trước khi có
    `CatalogSpec.update_guard` thì đường sửa rơi xuống `CHECK` và trả về
    `ck_items_default_warehouse_needs_stock_nature` (review H-4).
    """
    created = _create_item(client, editor, nature=ItemNature.SERVICE.value)
    assert created.status_code == 201, created.text
    item = created.json()

    response = client.put(
        f"/api/v1/master/{ITEMS}/{item['id']}",
        json={
            "row_version": item["row_version"],
            "code": str(item["code"]),
            "name": str(item["name"]),
            "name_en": None,
            "is_active": True,
            "warehouse_id": _warehouse(client, editor),
            "description": None,
        },
        headers=editor,
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error_code"] == "item.warehouse_not_allowed"
    assert "constraint" not in body.get("details", {})


def test_updating_a_goods_item_still_accepts_a_warehouse(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đối trọng: `update_guard` không được chặn ca hợp lệ."""
    item = _goods(client, editor, _unit(client, editor))
    warehouse = _warehouse(client, editor)

    response = client.put(
        f"/api/v1/master/{ITEMS}/{item['id']}",
        json={
            "row_version": item["row_version"],
            "code": str(item["code"]),
            "name": str(item["name"]),
            "name_en": None,
            "is_active": True,
            "warehouse_id": warehouse,
            "description": None,
        },
        headers=editor,
    )

    assert response.status_code == 200, response.text
    assert response.json()["warehouse_id"] == warehouse


# --------------------------------------------------- cơ chế của đường thêm/sửa (M-1, M-2)


def test_changing_a_conversion_unit_into_the_base_unit_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """`_ensure_convertible` phải chạy ở **cả** đường sửa (đột biến M04 sống sót)."""
    base_unit = _unit(client, editor)
    item = _goods(client, editor, base_unit)
    row = _add_unit(client, editor, int(item["id"]), _unit(client, editor), factor="24")
    assert row.status_code == 201, row.text

    response = client.put(
        f"{_units_url(int(item['id']))}/{row.json()['id']}",
        json={"row_version": row.json()["row_version"], "unit_id": base_unit, "factor": "24"},
        headers=editor,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "item.unit_duplicates_base"


@pytest.mark.parametrize("verb", ["add", "update"])
def test_a_conversion_unit_pointing_at_a_group_unit_is_refused(
    client: TestClient, editor: dict[str, str], verb: str
) -> None:
    """`ensure_catalog_choice` phải chạy ở cả hai đường ghi (M17/M18 sống sót)."""
    item = _goods(client, editor, _unit(client, editor))
    group_unit = _unit(client, editor, is_group=True)

    if verb == "add":
        response = _add_unit(client, editor, int(item["id"]), group_unit, factor="24")
    else:
        row = _add_unit(client, editor, int(item["id"]), _unit(client, editor), factor="24")
        assert row.status_code == 201, row.text
        response = client.put(
            f"{_units_url(int(item['id']))}/{row.json()['id']}",
            json={
                "row_version": row.json()["row_version"],
                "unit_id": group_unit,
                "factor": "24",
            },
            headers=editor,
        )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "master_data.group_not_postable"


def test_a_stale_row_version_on_a_variant_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Khóa lạc quan (FR-NFR-005) của quy cách — đột biến M16 sống sót vì thiếu test này."""
    item = _goods(client, editor, _unit(client, editor))
    variant = _add_variant(client, editor, int(item["id"]), code="DO")
    assert variant.status_code == 201, variant.text

    response = client.put(
        f"{_variants_url(int(item['id']))}/{variant.json()['id']}",
        json={"row_version": 99, "code": "DO", "name": "Đỏ", "is_active": True},
        headers=editor,
    )

    assert response.status_code == 409, response.text


def test_a_negative_factor_is_refused_at_the_api_layer_not_by_the_database(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Hai lớp phòng thủ của `factor` che nhau nếu chỉ khẳng định mã trạng thái.

    `CHECK factor > 0` của DB cũng trả `422`, nên đột biến gỡ `gt=0` khỏi biên API
    **sống sót** (review M-3). Thứ phân biệt hai lớp là thân phản hồi: lớp API trả
    lỗi xác thực thân request, lớp DB trả `details.constraint`.
    """
    item = _goods(client, editor, _unit(client, editor))

    response = _add_unit(client, editor, int(item["id"]), _unit(client, editor), factor="-1")

    assert response.status_code == 422, response.text
    assert "constraint" not in response.json().get("details", {})


def test_a_variant_code_of_only_spaces_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Mã quy cách là **trục khóa** của báo cáo tồn kho nên `"DO"` và `"DO "` không
    được là hai quy cách (review M-4)."""
    item = _goods(client, editor, _unit(client, editor))

    blank = _add_variant(client, editor, int(item["id"]), code="   ", name="  ")
    assert blank.status_code == 422, blank.text

    first = _add_variant(client, editor, int(item["id"]), code="DO")
    assert first.status_code == 201, first.text
    padded = _add_variant(client, editor, int(item["id"]), code=" DO ")
    assert padded.status_code == 409, padded.text


def test_one_key_on_two_items_does_not_swallow_the_second_row(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Khóa idempotency phải phân biệt hai mã hàng chủ (review M-6).

    `route_key` chứa `{item_id}` theo **nghĩa đen** nên nó không phân biệt; nếu
    `item_id` cũng không có trong vân tay thì lượt thứ hai bị coi là phát lại,
    `replay` tìm dòng cũ dưới mã hàng mới và trả `404` — dòng không bao giờ được
    tạo, im lặng.
    """
    base_unit = _unit(client, editor)
    first_item = _goods(client, editor, base_unit)
    second_item = _goods(client, editor, base_unit)
    box = _unit(client, editor)
    key = unique_code("KEY")
    payload = {"unit_id": box, "factor": "24"}

    first = client.post(
        _units_url(int(first_item["id"])), json=payload, headers={**editor, IDEMPOTENCY_HEADER: key}
    )
    second = client.post(
        _units_url(int(second_item["id"])),
        json=payload,
        headers={**editor, IDEMPOTENCY_HEADER: key},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "idempotency.key_reused"


# ------------------------------------------ gộp đơn vị tính: từ chối khi lệch (C1)


def _declare(
    client: TestClient,
    headers: dict[str, str],
    *,
    base_unit: int,
    other_unit: int,
    factor: str,
) -> dict[str, object]:
    """Một mã hàng khai `1 other_unit = factor × base_unit` — một lời khẳng định
    về tỷ lệ giữa hai đơn vị, nằm sẵn trong DB."""
    item = _goods(client, headers, base_unit)
    row = _add_unit(client, headers, int(item["id"]), other_unit, factor=factor)
    assert row.status_code == 201, row.text
    return item


def test_merging_units_that_the_data_proves_are_different_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """C1 — lỗi nặng nhất của lát này, tìm ra bằng probe chứ không bằng đột biến.

    Gộp hai đơn vị tính là đường ghi **thứ hai** vào `items.base_unit_id`: câu
    `UPDATE` chung của `merge_service` trỏ cột đó sang đơn vị đích, đi vòng qua
    đúng điều H69 chốt một lần để bảo vệ. Bản đầu tiên cho gộp `S` vào `T` dù mã
    hàng Y đang khai `1 S = 1000 T`, và mã hàng **X** — không hề bị nhắc tới
    trong lần gộp — từ đó đọc là `1 A = 24 T` trong khi sự thật là `24000`.
    """
    source_unit = _unit(client, editor)
    target_unit = _unit(client, editor)
    # Y khẳng định hai đơn vị lệch nhau 1000 lần.
    _declare(client, editor, base_unit=target_unit, other_unit=source_unit, factor="1000")
    # X lấy đơn vị nguồn làm đơn vị chính — nạn nhân của lần gộp.
    victim = _declare(
        client, editor, base_unit=source_unit, other_unit=_unit(client, editor), factor="24"
    )

    response = _merge(client, editor, UNITS, source_id=source_unit, target_id=target_unit)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "master_data.merge_refused"
    assert body["details"]["reason"] == "unit_factor_conflicts"
    # Và nạn nhân không hề bị đụng tới.
    detail = client.get(f"/api/v1/master/{ITEMS}/{victim['id']}", headers=editor).json()
    assert detail["base_unit_id"] == source_unit
    rows = client.get(_units_url(int(victim["id"])), headers=editor).json()["items"]
    assert [Decimal(row["factor"]) for row in rows] == [Decimal(24)]


def test_merging_units_declared_with_different_factors_on_one_item_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Hình dạng thứ hai của cùng bằng chứng: một mã hàng khai **cả hai** đơn vị.

    Hai tỷ lệ cùng quy về đơn vị chính của nó, nên nếu hai đơn vị là một thì hai
    tỷ lệ phải bằng nhau.
    """
    base_unit = _unit(client, editor)
    source_unit = _unit(client, editor)
    target_unit = _unit(client, editor)
    item = _goods(client, editor, base_unit)
    assert _add_unit(client, editor, int(item["id"]), source_unit, factor="24").status_code == 201
    assert _add_unit(client, editor, int(item["id"]), target_unit, factor="12").status_code == 201

    response = _merge(client, editor, UNITS, source_id=source_unit, target_id=target_unit)

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "unit_factor_conflicts"


def test_merging_units_the_data_proves_equal_still_works(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Đối trọng: "cái" và "chiếc" thật sự trùng (tỷ lệ 1) vẫn gộp được.

    Không có test này thì phép từ chối ở trên có thể là một phép từ chối **mọi
    thứ**, và gộp đơn vị tính — nơi trùng lặp xảy ra nhiều nhất khi chuyển dữ liệu
    từ phần mềm cũ — sẽ chết hẳn mà bộ test vẫn xanh.
    """
    source_unit = _unit(client, editor)
    target_unit = _unit(client, editor)
    item = _declare(client, editor, base_unit=target_unit, other_unit=source_unit, factor="1")

    response = _merge(client, editor, UNITS, source_id=source_unit, target_id=target_unit)

    assert response.status_code == 200, response.text
    detail = client.get(f"/api/v1/master/{ITEMS}/{item['id']}", headers=editor).json()
    assert detail["base_unit_id"] == target_unit
    # Dòng "1 nguồn = 1 đích" trở thành "đơn vị chính quy về chính nó" ⇒ bị bỏ...
    assert client.get(_units_url(int(item["id"])), headers=editor).json()["items"] == []
    # ...và báo cáo **không** đếm nó là "đã chuyển" (review L-1).
    assert response.json()["total_rows_moved"] == 0


# --------------------------------------------------- không gộp vào nút nhóm (H-2)


def test_merging_into_a_group_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Gộp **vào** nhóm chôn sống bảng con và cắm khóa ngoại vào một nút nhóm.

    Router bảng con từ chối nhóm (`load_item`), nên hai dòng con chuyển sang nhóm
    không đọc, không sửa, không xóa được bằng endpoint nào — chúng chỉ chết theo
    `CASCADE`. Phép kiểm nằm ở `merge_records` nên nó che cho **mọi** danh mục.
    """
    base_unit = _unit(client, editor)
    source = _goods(client, editor, base_unit)
    assert _add_variant(client, editor, int(source["id"]), code="DO").status_code == 201
    group = _create_item(client, editor, is_group=True)
    assert group.status_code == 201, group.text

    response = _merge(
        client, editor, ITEMS, source_id=int(source["id"]), target_id=int(group.json()["id"])
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "master_data.merge_refused"
    assert body["details"]["reason"] == "target_is_group"


def test_merging_a_unit_into_a_group_unit_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Kịch bản B của H-2, độc lập với vật tư: `base_unit_id` cắm vào nút nhóm là
    trạng thái mà `ensure_catalog_choice` từ chối tạo — và vì cột chốt một lần,
    không có đường nào sửa lại."""
    source_unit = _unit(client, editor)
    group_unit = _unit(client, editor, is_group=True)
    item = _goods(client, editor, source_unit)

    response = _merge(client, editor, UNITS, source_id=source_unit, target_id=group_unit)

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "target_is_group"
    detail = client.get(f"/api/v1/master/{ITEMS}/{item['id']}", headers=editor).json()
    assert detail["base_unit_id"] == source_unit


# ------------------------------------- cô lập chi nhánh của hai bảng con (H-3)


@pytest.fixture
def two_branch_editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_role: str,
    item_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Người dùng được gán **hai** chi nhánh — phải gửi `X-Branch` để nói đang ở đâu."""
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_role,
        "vattuhai",
        test_password,
        branch_codes=item_branches,
    )


def _private_item_of_branch_a(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    item_branches: list[str],
) -> tuple[int, dict[str, str], dict[str, str]]:
    """Mã hàng **riêng** chi nhánh A, kèm header của A và của B."""
    ids = branch_ids(session_factory, dataset_alpha, item_branches)
    at_a = {**two_branch_editor, BRANCH_HEADER: str(ids[item_branches[0]])}
    at_b = {**two_branch_editor, BRANCH_HEADER: str(ids[item_branches[1]])}
    unit = create_record(client, at_a, UNITS, {"code": unique_code("DVT"), "name": "Đơn vị của A"})
    assert unit.status_code == 201, unit.text
    item = _create_item(
        client,
        at_a,
        nature=ItemNature.GOODS.value,
        base_unit_id=int(unit.json()["id"]),
        branch_id=ids[item_branches[0]],
    )
    assert item.status_code == 201, item.text
    return int(item.json()["id"]), at_a, at_b


def test_the_child_tables_of_another_branch_item_are_out_of_reach(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    item_branches: list[str],
) -> None:
    """Cả **tám** endpoint bảng con phải trả `404` khi mã hàng chủ thuộc chi nhánh khác.

    Hai bảng con không có `branch_id` và không bật RLS, nên `ensure_visible` trong
    `load_item` là lớp cô lập chi nhánh **duy nhất** của chúng. Bộ test đầu tiên
    của lát này dùng một fixture có phạm vi toàn công ty và không đặt `X-Branch`
    bao giờ, nên đột biến xóa hẳn `ensure_visible` **sống sót** (review H-3, đột
    biến M12) — hành vi đúng nhưng không gì canh. Đây là lần thứ ba điểm mù ấy
    lặp lại, nên test liệt kê từng đường một thay vì tin vào một đường đại diện.
    """
    item_id, at_a, at_b = _private_item_of_branch_a(
        client, two_branch_editor, session_factory, dataset_alpha, item_branches
    )
    unit = _unit(client, at_a)
    row = _add_unit(client, at_a, item_id, unit, factor="24")
    variant = _add_variant(client, at_a, item_id, code="DO")
    assert row.status_code == 201 and variant.status_code == 201, variant.text
    row_id = int(row.json()["id"])
    variant_id = int(variant.json()["id"])

    attempts = {
        "GET units": client.get(_units_url(item_id), headers=at_b),
        "POST units": _add_unit(client, at_b, item_id, _unit(client, at_a), factor="12"),
        "PUT units": client.put(
            f"{_units_url(item_id)}/{row_id}",
            json={"row_version": 1, "unit_id": unit, "factor": "12"},
            headers=at_b,
        ),
        "DELETE units": client.delete(f"{_units_url(item_id)}/{row_id}", headers=at_b),
        "GET variants": client.get(_variants_url(item_id), headers=at_b),
        "POST variants": _add_variant(client, at_b, item_id, code="XANH"),
        "PUT variants": client.put(
            f"{_variants_url(item_id)}/{variant_id}",
            json={"row_version": 1, "code": "DO", "name": "Đỏ", "is_active": True},
            headers=at_b,
        ),
        "DELETE variants": client.delete(f"{_variants_url(item_id)}/{variant_id}", headers=at_b),
    }

    refused = {name: response.status_code for name, response in attempts.items()}
    assert refused == dict.fromkeys(attempts, 404), refused
    # Và dữ liệu của chi nhánh A còn nguyên sau tám lần thử.
    assert len(client.get(_units_url(item_id), headers=at_a).json()["items"]) == 1
    assert len(client.get(_variants_url(item_id), headers=at_a).json()["items"]) == 1


def test_an_idempotency_replay_from_another_branch_is_refused(
    client: TestClient,
    two_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    item_branches: list[str],
) -> None:
    """Lượt **phát lại** không được là đường vòng qua phép kiểm chi nhánh.

    Người dùng đổi chi nhánh đang thao tác giữa hai lần gửi cùng một khóa: lượt
    thứ hai đi vào nhánh `replay`, và nếu nhánh ấy không nạp lại mã hàng chủ thì
    nó trả về một bản ghi mà đường `GET` vừa từ chối.
    """
    item_id, at_a, at_b = _private_item_of_branch_a(
        client, two_branch_editor, session_factory, dataset_alpha, item_branches
    )
    key = unique_code("KEY")
    payload = {"unit_id": _unit(client, at_a), "factor": "24"}

    first = client.post(
        _units_url(item_id), json=payload, headers={**at_a, IDEMPOTENCY_HEADER: key}
    )
    replay = client.post(
        _units_url(item_id), json=payload, headers={**at_b, IDEMPOTENCY_HEADER: key}
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 404, replay.text
