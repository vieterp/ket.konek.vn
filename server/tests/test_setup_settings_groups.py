"""BFF màn hình Thiết lập: hai nhóm theo hệ quả (U14, FR-SYS-004 — lát 3B-2).

Điều đáng kiểm ở đây không phải "endpoint trả 200" mà là **luật phân nhóm sống ở
một chỗ**: một khóa tùy chọn khai `decided_once` phải rơi vào nhóm chốt-một-lần
mà không ai phải sửa thêm gì ở tầng API, và quyết định của năm tài chính phải
khóa lại khi năm đã quyết toán.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_branches, ensure_role
from conftest import api_test_client
from ket.api.routers.setup_schemas import ANYTIME_GROUP, DECIDED_ONCE_GROUP
from ket.kernel.config.catalog import CATALOG, MONEY_SCALE_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.periods.models import (
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

URL = "/api/v1/setup/settings-groups"

READER_ROLE = "xem_thiet_lap"
NO_SETTINGS_ROLE = "khong_xem_thiet_lap"
BRANCH_CODES = ["CN_TL_A"]

CLOSED_YEAR_CODE = "TL2019"
"""Một niên độ **đã quyết toán**, tạo riêng cho tệp này. Mã lệch hẳn năm hiện
tại để không đụng năm mà test khác dựng."""


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def setup_branches(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> list[str]:
    return ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        READER_ROLE,
        [permission_code(SYSTEM_MODULE, "setting", Action.VIEW)],
    )


@pytest.fixture(scope="module")
def role_without_settings(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Vai trò có quyền khác nhưng **không** có quyền xem thiết lập."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        NO_SETTINGS_ROLE,
        [permission_code(SYSTEM_MODULE, "branch", Action.VIEW)],
    )


@pytest.fixture
def reader(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    reader_role: str,
    setup_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        reader_role,
        "thietlap",
        test_password,
        branch_codes=setup_branches,
    )


@pytest.fixture
def closed_year(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Một năm tài chính đã quyết toán, dựng một lần rồi dùng lại."""
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        year = session.scalars(
            select(FiscalYear).where(FiscalYear.code == CLOSED_YEAR_CODE)
        ).first()
        if year is None:
            year = PeriodService(session).create_fiscal_year(
                code=CLOSED_YEAR_CODE,
                start_date=date(2019, 1, 1),
                accounting_scheme=AccountingScheme.TT99,
                base_currency="VND",
                inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_PERIOD,
                vat_method=VatMethod.DEDUCTION,
            )
        year.is_closed = True
    return CLOSED_YEAR_CODE


def _groups(client: TestClient, headers: dict[str, str]) -> dict[str, list[dict[str, object]]]:
    response = client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    return {group["key"]: group["items"] for group in response.json()["groups"]}


def test_the_response_has_exactly_the_two_groups(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Hai nhóm, không hơn: màn hình dựng từ đây nên nhóm thứ ba là một tab không ai thiết kế."""
    groups = _groups(client, reader)

    assert set(groups) == {ANYTIME_GROUP, DECIDED_ONCE_GROUP}


def test_a_setting_marked_decided_once_lands_in_that_group(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Luật phân nhóm đọc từ `SettingDefinition.decided_once`, không từ một danh sách ở API.

    `money.scale` là khóa duy nhất mang cờ đó hôm nay. Nếu tầng API giữ danh sách
    riêng thì khóa thứ hai mang cờ ở phase sau sẽ rơi nhầm nhóm — và nhóm sai
    luôn là nhóm "đổi thoải mái".
    """
    groups = _groups(client, reader)

    decided_keys = {str(item["key"]) for item in groups[DECIDED_ONCE_GROUP]}
    anytime_keys = {str(item["key"]) for item in groups[ANYTIME_GROUP]}

    assert MONEY_SCALE_KEY in decided_keys
    assert MONEY_SCALE_KEY not in anytime_keys
    expected_anytime = {key for key, definition in CATALOG.items() if not definition.decided_once}
    assert expected_anytime <= anytime_keys


def test_every_catalog_setting_appears_exactly_once(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Không khóa nào rơi ra ngoài cả hai nhóm — màn hình Thiết lập là nơi duy nhất sửa chúng."""
    groups = _groups(client, reader)
    shown = [
        str(item["key"])
        for items in groups.values()
        for item in items
        if item["source"] == "setting"
    ]

    assert sorted(shown) == sorted(CATALOG)


def test_a_closed_fiscal_year_is_reported_as_locked(
    client: TestClient, reader: dict[str, str], closed_year: str
) -> None:
    """Năm đã quyết toán thì bốn quyết định của nó không sửa được nữa (FR-SYS-004)."""
    groups = _groups(client, reader)

    of_year = [
        item for item in groups[DECIDED_ONCE_GROUP] if item["fiscal_year_code"] == closed_year
    ]

    assert len(of_year) == 4, "bốn quyết định chốt một lần của mỗi niên độ"
    assert all(item["is_editable"] is False for item in of_year)
    assert all(item["locked_reason"] for item in of_year)


def test_an_open_fiscal_year_is_still_editable(
    client: TestClient,
    reader: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Đối trọng: không có nó thì một cột `is_editable` luôn `False` vẫn xanh."""
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        open_years = list(
            session.scalars(select(FiscalYear.code).where(FiscalYear.is_closed.is_(False))).all()
        )
        if not open_years:
            created = PeriodService(session).create_fiscal_year(
                code="TL2018",
                start_date=date(2018, 1, 1),
                accounting_scheme=AccountingScheme.TT133,
                base_currency="VND",
                inventory_valuation_method=InventoryValuationMethod.FIFO,
                vat_method=VatMethod.DIRECT,
            )
            open_years = [created.code]

    groups = _groups(client, reader)
    editable = [
        item for item in groups[DECIDED_ONCE_GROUP] if item["fiscal_year_code"] in open_years
    ]

    assert editable, "không tìm thấy niên độ đang mở nào trong phản hồi"
    assert all(item["is_editable"] is True for item in editable)
    assert all(item["locked_reason"] is None for item in editable)


def test_reading_the_groups_needs_the_settings_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    role_without_settings: str,
    setup_branches: list[str],
    test_password: str,
) -> None:
    """Thiết lập lộ ra chế độ kế toán và cách tính giá xuất — không phải dữ liệu công khai."""
    outsider = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role_without_settings,
        "khongxem",
        test_password,
        branch_codes=setup_branches,
    )

    response = client.get(URL, headers=outsider)

    assert response.status_code == 403, response.text
