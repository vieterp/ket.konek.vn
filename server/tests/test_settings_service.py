"""Tùy chọn hai cấp ở tầng dịch vụ (FR-SYS-060, BR-SYS-06).

Hai nhóm: bộ kiểm giá trị của catalog (không cần DB) và phép phân giải ba tầng
(cần DB thật, vì nó là một câu truy vấn trên bảng có RLS và ràng buộc bộ phận).
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config import settings_service
from ket.kernel.config.catalog import (
    CATALOG,
    GRID_ENTER_KEY,
    LOCALE_KEY,
    MONEY_SCALE_KEY,
    SettingDefinition,
    SettingScope,
    ValueType,
    parse_value,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    RowVersionConflictError,
    SettingScopeNotAllowedError,
    SettingUnknownError,
    SettingValueInvalidError,
)
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

# --------------------------------------------------------------------------
# Catalog: kiểu và ràng buộc (không cần DB)
# --------------------------------------------------------------------------


def test_every_catalog_default_survives_its_own_validation() -> None:
    """Mặc định sai sẽ chỉ lộ ra khi có người đầu tiên đọc khóa đó."""
    for definition in CATALOG.values():
        parse_value(definition, definition.default)


def test_a_boolean_setting_accepts_only_the_two_canonical_words() -> None:
    """`"True"`, `"1"`, `"yes"` đều bị từ chối — một dạng lưu duy nhất.

    Nhận nhiều dạng nghĩa là bảng sẽ có cả bốn dạng sau vài phase, và mỗi nơi
    đọc lại phải tự đoán dạng nào.
    """
    definition = CATALOG[GRID_ENTER_KEY]

    assert parse_value(definition, "true") is True
    assert parse_value(definition, "false") is False
    for rejected in ("True", "1", "yes", ""):
        with pytest.raises(SettingValueInvalidError):
            parse_value(definition, rejected)


def test_an_integer_setting_is_bounded_by_its_declaration() -> None:
    definition = CATALOG[MONEY_SCALE_KEY]

    assert parse_value(definition, "0") == 0
    with pytest.raises(SettingValueInvalidError):
        parse_value(definition, "9")
    with pytest.raises(SettingValueInvalidError):
        parse_value(definition, "-1")
    with pytest.raises(SettingValueInvalidError):
        parse_value(definition, "hai")


def test_a_string_setting_with_choices_refuses_anything_else() -> None:
    definition = CATALOG[LOCALE_KEY]

    assert parse_value(definition, "en") == "en"
    with pytest.raises(SettingValueInvalidError):
        parse_value(definition, "fr")


def test_a_decimal_setting_never_goes_through_float() -> None:
    """Giá trị thập phân đọc ra phải là `Decimal` — cùng luật với tiền (LD-03)."""
    definition = SettingDefinition(
        key="test.ty_le",
        value_type=ValueType.DECIMAL,
        default="0.1",
        scopes=frozenset({SettingScope.SYSTEM}),
        description="Chỉ dùng trong test",
    )

    value = parse_value(definition, "0.1")

    assert isinstance(value, Decimal)
    assert value == Decimal("0.1")


# --------------------------------------------------------------------------
# Phân giải ba tầng (cần PostgreSQL)
# --------------------------------------------------------------------------


def _scope(dataset: DatasetRef, user_id: int) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=user_id, branch_ids=())


@pytest.mark.db
def test_the_user_value_wins_over_the_system_value_which_wins_over_the_default(
    session_factory: sessionmaker[Session], dataset_beta: DatasetRef
) -> None:
    """Ba tầng, xét từ hẹp tới rộng — và mỗi tầng phải khai đúng nguồn của nó."""
    with unit_of_work(session_factory, _scope(dataset_beta, 41)) as session:
        default = settings_service.effective_settings(session, user_id=41)
        by_key = {item.key: item for item in default}
        assert by_key[LOCALE_KEY].source == "default"
        assert by_key[LOCALE_KEY].value == "vi"

        settings_service.set_setting(
            session,
            key=LOCALE_KEY,
            scope=SettingScope.SYSTEM,
            user_id=41,
            raw_value="en",
            expected_row_version=None,
        )
        after_system = settings_service.value_of(session, key=LOCALE_KEY, user_id=41)
        assert after_system == "en"

        settings_service.set_setting(
            session,
            key=LOCALE_KEY,
            scope=SettingScope.USER,
            user_id=41,
            raw_value="vi",
            expected_row_version=None,
        )

        assert settings_service.value_of(session, key=LOCALE_KEY, user_id=41) == "vi"
        # Người khác vẫn thấy giá trị chung: tùy chọn riêng không rò sang ai.
        assert settings_service.value_of(session, key=LOCALE_KEY, user_id=42) == "en"


@pytest.mark.db
def test_an_unknown_key_and_a_forbidden_scope_are_refused(
    session_factory: sessionmaker[Session], dataset_beta: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_beta, 43)) as session:
        with pytest.raises(SettingUnknownError):
            settings_service.set_setting(
                session,
                key="khoa.bia",
                scope=SettingScope.SYSTEM,
                user_id=43,
                raw_value="1",
                expected_row_version=None,
            )
        with pytest.raises(SettingScopeNotAllowedError):
            settings_service.set_setting(
                session,
                key=MONEY_SCALE_KEY,
                scope=SettingScope.USER,
                user_id=43,
                raw_value="3",
                expected_row_version=None,
            )


@pytest.mark.db
def test_saving_on_a_stale_version_reports_the_current_value(
    session_factory: sessionmaker[Session], dataset_beta: DatasetRef
) -> None:
    """Xung đột phải mang theo giá trị hiện tại, nếu không người dùng chỉ biết "hỏng"."""
    with unit_of_work(session_factory, _scope(dataset_beta, 44)) as session:
        settings_service.set_setting(
            session,
            key=GRID_ENTER_KEY,
            scope=SettingScope.USER,
            user_id=44,
            raw_value="false",
            expected_row_version=None,
        )

        with pytest.raises(RowVersionConflictError) as conflict:
            settings_service.set_setting(
                session,
                key=GRID_ENTER_KEY,
                scope=SettingScope.USER,
                user_id=44,
                raw_value="true",
                expected_row_version=None,
            )

    latest = conflict.value.problem_extra()["latest"]
    assert latest["value"] == "false"
    assert latest["row_version"] == 1
    assert latest["scope"] == "user"


@pytest.mark.db
def test_two_parallel_first_writes_do_not_collide(
    app_engine: Engine, dataset_beta: DatasetRef
) -> None:
    """Lần ghi **đầu tiên** cho một khóa là chỗ dễ hỏng nhất.

    Chưa có dòng nào để `SELECT … FOR UPDATE` khóa, nên nếu không có khóa cố
    vấn thì bốn luồng cùng thấy trống, cùng `INSERT`, và ba trong số đó nhận
    `UniqueViolation` — tức là `500` cho một thao tác thiết lập bình thường.
    """
    factory = create_session_factory(app_engine)

    def write(index: int) -> str:
        with unit_of_work(factory, _scope(dataset_beta, 50 + index)) as session:
            result = settings_service.set_setting(
                session,
                key=GRID_ENTER_KEY,
                scope=SettingScope.SYSTEM,
                user_id=50 + index,
                raw_value="true" if index % 2 else "false",
                # Mọi luồng đều tin rằng chưa có dòng nào — đúng như bốn tab
                # trình duyệt cùng mở màn hình thiết lập lúc chưa ai cấu hình.
                expected_row_version=None,
            )
            return result.raw_value

    with ThreadPoolExecutor(max_workers=4) as pool:
        # Gửi hết rồi mới chờ: `_settle(pool.submit(...))` trong một
        # comprehension sẽ chờ từng luồng xong mới gửi luồng kế, tức là chạy
        # tuần tự — và test sẽ xanh kể cả khi khóa cố vấn bị gỡ.
        futures = [pool.submit(write, index) for index in range(4)]
        outcomes = [_settle(future) for future in futures]

    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, Exception)]

    assert len(winners) == 1, f"đúng một luồng được ghi lần đầu, nhận {outcomes}"
    assert all(isinstance(item, RowVersionConflictError) for item in conflicts), (
        f"luồng thua phải nhận xung đột phiên bản, không phải lỗi DB thô: {conflicts}"
    )


def _settle(future: Future[str]) -> str | Exception:
    """Kết quả **hoặc** ngoại lệ của một luồng — test cần phân loại cả hai nhánh."""
    try:
        return future.result()
    except Exception as error:
        return error
