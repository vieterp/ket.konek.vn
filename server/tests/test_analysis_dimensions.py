"""Chiều phân tích mở rộng: gieo mầm, khai lúc chạy, và luật phạm vi (lát 3B-1).

Ba nhóm khẳng định, theo thứ tự rủi ro:

1. **Gieo mầm** — "Mã thống kê" (FR-SYS-051) có mặt trong **mọi** dữ liệu kế
   toán vừa cấp, và gieo lại không đè lên thứ khách hàng đã sửa.
2. **Khai bằng cấu hình** — thêm một chiều là thêm một dòng, không migration.
   Đây là tiêu chí thành công của phase 3, nên nó phải có test chứ không chỉ có
   một câu trong tài liệu.
3. **Phạm vi** — mã giá trị chỉ duy nhất **trong một chiều**, nên mọi đường tra
   phải đi qua chiều. Ba chỗ dễ quên: `resolve_value`, `subtree_of` (tiền tố
   `path` dùng chung một sequence với chiều khác), và `parent_id` lúc thêm giá
   trị.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.dimensions import seed as seed_module
from ket.kernel.dimensions.models import AnalysisDimension, DimensionValueSource
from ket.kernel.dimensions.seed import STATISTICAL_CODE_DIMENSION, ensure_builtin_dimensions
from ket.kernel.dimensions.service import DimensionService, validate_value_source
from ket.kernel.errors import (
    DimensionNotFoundError,
    DimensionValueNotFoundError,
    DimensionValueSourceMismatchError,
)
from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.registry import CatalogRegistry, CatalogSpec
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Permission
from ket.kernel.security.permissions import (
    MASTER_MODULE,
    Action,
    PermissionRegistry,
    permission_code,
)

pytestmark = pytest.mark.db


@pytest.fixture
def session(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> Iterator[Session]:
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as active:
        yield active


def _unique(prefix: str, session: Session) -> str:
    """Mã chưa dùng — bộ test dùng chung một dataset với các tệp khác."""
    existing = set(session.scalars(select(AnalysisDimension.code)).all())
    index = 1
    while f"{prefix}{index}" in existing:
        index += 1
    return f"{prefix}{index}"


# --------------------------------------------------------------- gieo mầm


def test_the_statistical_code_dimension_exists_in_a_fresh_dataset(session: Session) -> None:
    """FR-SYS-051 có mặt ngay khi dữ liệu kế toán vừa được cấp, không phải sau đó.

    Gói cấu hình phase 5 và mẫu báo cáo tham chiếu chiều này **theo mã**, nên nó
    là một hợp đồng: thiếu nó thì báo cáo hỏng ở khách hàng, không ở CI.
    """
    dimension = DimensionService(session).get_by_code(STATISTICAL_CODE_DIMENSION)

    assert dimension.name == "Mã thống kê"
    assert dimension.value_source is DimensionValueSource.LIST
    assert dimension.master_slug is None
    assert dimension.is_active is True


def test_the_seed_does_not_carry_any_values(session: Session) -> None:
    """Gieo **định nghĩa** chiều, không gieo giá trị.

    "Mã thống kê" là chiều tự do mỗi doanh nghiệp tự đổ giá trị vào; đoán hộ
    "miền Bắc / miền Nam" là đoán sai với doanh nghiệp phân theo kênh bán.
    """
    service = DimensionService(session)
    dimension = service.get_by_code(STATISTICAL_CODE_DIMENSION)

    assert list(service.values_of(dimension)) == []


def test_seeding_twice_does_not_overwrite_a_renamed_dimension(
    owner_engine: Engine, dataset_alpha: DatasetRef, session_factory: sessionmaker[Session]
) -> None:
    """Chạy lại được, và **thêm-nếu-thiếu** chứ không ghi đè.

    Khách hàng được đổi tên hiển thị của chiều này (nhiều nơi gọi nó là "mã
    phân tích"), và một lần nâng cấp không được lặng lẽ đặt lại tên đó.
    """
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        dimension = DimensionService(session).get_by_code(STATISTICAL_CODE_DIMENSION)
        dimension.name = "Mã phân tích của doanh nghiệp"

    with owner_engine.begin() as connection:
        added = ensure_builtin_dimensions(connection, dataset_alpha.schema_name)
    assert added == 0

    with unit_of_work(session_factory, scope) as session:
        assert (
            DimensionService(session).get_by_code(STATISTICAL_CODE_DIMENSION).name
            == "Mã phân tích của doanh nghiệp"
        )


def test_catalog_permissions_are_seeded_into_a_fresh_dataset(session: Session) -> None:
    """Đăng ký danh mục lúc import phải tới được bảng `permissions` lúc cấp dataset.

    Đây là cổng cho một thứ chỉ hỏng theo **thứ tự import**: `role_service` đọc
    `PermissionRegistry` toàn cục, và mã quyền danh mục chỉ có mặt ở đó nếu
    `ket.kernel.master_data` đã được nạp. Lập luận thì đúng, nhưng thứ tự import
    là loại thứ đổi ngầm khi ai đó dọn dẹp một dòng `import` trông có vẻ thừa —
    nên nó cần một phép đo, không cần một lời giải thích.
    """
    seeded = set(session.scalars(select(Permission.code)).all())

    assert permission_code(MASTER_MODULE, "warehouses", Action.CREATE) in seeded
    assert permission_code(MASTER_MODULE, "analysis_dimension", Action.CREATE) in seeded


# ------------------------------------------------- khai bằng cấu hình


def test_declaring_a_dimension_needs_no_migration(session: Session) -> None:
    """Tiêu chí "chiều mở rộng khai bằng cấu hình, không sửa code" (RT-20)."""
    service = DimensionService(session)
    code = _unique("KENH", session)

    dimension = service.declare(
        code=code,
        name="Kênh bán hàng",
        name_en="Sales channel",
        is_required=True,
        applies_to_accounts=["511", "632"],
    )

    assert dimension.id is not None
    assert dimension.applies_to_accounts == ["511", "632"]
    # Cột `is_required`/`applies_to_accounts` **lưu** được từ v1; phần **ép**
    # thuộc posting engine phase 4 (RT-20 hoãn tới v1.1).
    assert dimension.is_required is True
    assert service.get_by_code(code).id == dimension.id


def test_values_form_a_tree_that_rolls_up(session: Session) -> None:
    """Giá trị có cây riêng để báo cáo cộng tổng theo nhánh."""
    service = DimensionService(session)
    dimension = service.declare(code=_unique("VUNG", session), name="Vùng")

    north = service.add_value(dimension, code="BAC", name="Miền Bắc")
    hanoi = service.add_value(dimension, code="HN", name="Hà Nội", parent=north)
    service.add_value(dimension, code="NAM", name="Miền Nam")

    assert north.level == 1
    assert north.path == f"{north.id}."
    assert hanoi.level == 2
    assert hanoi.path == f"{north.id}.{hanoi.id}."
    assert [value.code for value in service.subtree_of(north)] == ["BAC", "HN"]


def test_an_unknown_dimension_code_is_a_domain_error(session: Session) -> None:
    with pytest.raises(DimensionNotFoundError):
        DimensionService(session).get_by_code("KHONG_CO_THAT")


def test_inactive_values_are_hidden_from_the_default_value_list(session: Session) -> None:
    """Ô chọn trên form nhập liệu không được hiện giá trị đã ngừng theo dõi (L-4)."""
    service = DimensionService(session)
    dimension = service.declare(code=_unique("AN", session), name="Chiều có giá trị tắt")
    live = service.add_value(dimension, code="SONG", name="Còn dùng")
    retired = service.add_value(dimension, code="TAT", name="Ngừng theo dõi")
    retired.is_active = False
    session.flush()

    assert [value.id for value in service.values_of(dimension)] == [live.id]
    assert {value.id for value in service.values_of(dimension, include_inactive=True)} == {
        live.id,
        retired.id,
    }


def test_inactive_dimensions_are_hidden_from_the_default_listing(session: Session) -> None:
    """Nguồn dựng ô chọn phải ẩn chiều đã tắt, nếu không việc tắt chẳng có tác dụng gì."""
    service = DimensionService(session)
    dimension = service.declare(code=_unique("TAT", session), name="Chiều sẽ tắt")
    dimension.is_active = False
    session.flush()

    assert dimension.code not in {item.code for item in service.list_dimensions()}
    assert dimension.code in {item.code for item in service.list_dimensions(include_inactive=True)}


# ------------------------------------------------------------- phạm vi


def test_value_codes_are_scoped_to_their_dimension(session: Session) -> None:
    """Cùng một mã giá trị ở hai chiều là hợp lệ, và tra phải ra đúng chiều được hỏi.

    `BAC` là "miền Bắc" ở chiều vùng và "bán buôn cấp 1" ở chiều kênh. Một phép
    tra bỏ qua chiều sẽ trả về cái nào tình cờ đứng trước.
    """
    service = DimensionService(session)
    region = service.declare(code=_unique("V", session), name="Vùng")
    channel = service.declare(code=_unique("K", session), name="Kênh")
    service.add_value(region, code="BAC", name="Miền Bắc")
    service.add_value(channel, code="BAC", name="Bán buôn cấp 1")

    assert service.resolve_value(region.code, "BAC").name == "Miền Bắc"
    assert service.resolve_value(channel.code, "BAC").name == "Bán buôn cấp 1"


def test_a_subtree_never_leaks_into_another_dimension(session: Session) -> None:
    """`subtree_of` lọc `dimension_id` — phòng thủ **thừa một lớp**, cố ý giữ.

    Bản đầu của test này là phantom, và docstring của nó giải thích **ngược**
    (sửa sau review M-3). Sự thật: `add_value` lấy id từ `reserve_id(session,
    "analysis_dimension_values")` — **một** sequence cho cả bảng — nên id duy
    nhất toàn bảng và hai chiều không bao giờ có cùng `path`. Thêm nữa
    `like_prefix(p) = p + "%"`, nên `'3.%'` không khớp `'31.'` (dấu chấm cuối lo
    việc đó). Gỡ bộ lọc `dimension_id` hôm nay **không** làm hỏng gì — đó là lý
    do đột biến sống sót.

    Vậy tại sao giữ bộ lọc, và giữ test? Vì bất biến mà nó dựa vào nằm ở **chỗ
    khác**: khoảnh khắc `add_value` đổi sang sequence riêng cho mỗi chiều (một
    tối ưu hoàn toàn hợp lý), path sẽ đụng nhau và bộ lọc thành thứ duy nhất
    chặn báo cáo cộng nhầm. Test vì thế khẳng định **hành vi** đúng đó, và mô
    phỏng luôn tình huống path đụng nhau bằng cách dựng tay — chỗ này thì đột
    biến gỡ bộ lọc **có** chết.
    """
    service = DimensionService(session)
    first = service.declare(code=_unique("A", session), name="Chiều A")
    second = service.declare(code=_unique("B", session), name="Chiều B")

    root = service.add_value(first, code="R", name="Gốc A")
    intruder = service.add_value(second, code="R", name="Gốc B")

    # Ép hai chiều dùng chung một `path` — đúng trạng thái sẽ xảy ra nếu bảng
    # đổi sang sequence riêng cho mỗi chiều. Ghi thẳng qua ORM để đi vòng qua
    # `add_value`, vì chính `add_value` là thứ hôm nay giữ path không đụng nhau.
    intruder.path = root.path
    session.flush()

    subtree = service.subtree_of(root)

    assert {value.dimension_id for value in subtree} == {first.id}
    assert intruder.id not in {value.id for value in subtree}


def test_a_parent_from_another_dimension_is_refused(session: Session) -> None:
    """Một nhánh vắt qua hai chiều không có nghĩa nào cả, và DB không bắt được nó.

    Cả hai dòng đều hợp lệ khi xét riêng — chỉ quan hệ giữa chúng là sai, nên
    ràng buộc phải nằm ở dịch vụ.
    """
    service = DimensionService(session)
    first = service.declare(code=_unique("C", session), name="Chiều C")
    second = service.declare(code=_unique("D", session), name="Chiều D")
    foreign_parent = service.add_value(second, code="X", name="Giá trị chiều D")

    with pytest.raises(DimensionValueSourceMismatchError):
        service.add_value(first, code="Y", name="Con", parent=foreign_parent)

    with pytest.raises(DimensionValueNotFoundError):
        service.resolve_value_by_id(first, foreign_parent.id)


def test_an_unknown_value_code_is_a_domain_error(session: Session) -> None:
    service = DimensionService(session)
    dimension = service.declare(code=_unique("E", session), name="Chiều E")

    with pytest.raises(DimensionValueNotFoundError):
        service.resolve_value(dimension.code, "KHONG_CO")


# ----------------------------------------------------- nguồn giá trị


def test_a_master_backed_dimension_must_name_a_registered_catalog(session: Session) -> None:
    """`master_slug` là hằng trong mã nguồn — PostgreSQL không kiểm hộ được.

    Không kiểm ở tầng dịch vụ thì một slug gõ sai chỉ lộ ra khi có người mở ô
    chọn và thấy nó rỗng, thường là vài tuần sau lúc khai.
    """
    service = DimensionService(session)

    ok = service.declare(
        code=_unique("F", session),
        name="Theo đối tượng chi phí",
        value_source=DimensionValueSource.MASTER,
        master_slug="cost_objects",
    )
    assert ok.master_slug == "cost_objects"

    with pytest.raises(DimensionValueSourceMismatchError):
        service.declare(
            code=_unique("G", session),
            name="Trỏ vào hư không",
            value_source=DimensionValueSource.MASTER,
            master_slug="khong_co_danh_muc_nay",
        )


def test_the_two_source_columns_must_agree(session: Session) -> None:
    """`value_source` và `master_slug` là một cặp — nửa này không có nửa kia là vô nghĩa."""
    service = DimensionService(session)

    with pytest.raises(DimensionValueSourceMismatchError):
        service.declare(
            code=_unique("H", session),
            name="Thiếu slug",
            value_source=DimensionValueSource.MASTER,
        )

    with pytest.raises(DimensionValueSourceMismatchError):
        service.declare(
            code=_unique("I", session),
            name="Thừa slug",
            value_source=DimensionValueSource.LIST,
            master_slug="cost_objects",
        )


def test_a_master_backed_dimension_has_no_value_list_of_its_own(session: Session) -> None:
    """Hỏi danh sách giá trị của chiều lấy từ danh mục = hỏi nhầm chỗ.

    Trả lỗi chứ không trả danh sách rỗng: rỗng nghĩa là "chưa ai khai giá trị
    nào", và client sẽ hiện một ô chọn trống thay vì đi đọc
    `/api/v1/master/{master_slug}`.
    """
    service = DimensionService(session)
    dimension = service.declare(
        code=_unique("J", session),
        name="Theo đối tượng chi phí",
        value_source=DimensionValueSource.MASTER,
        master_slug="cost_objects",
    )

    with pytest.raises(DimensionValueSourceMismatchError):
        service.values_of(dimension)
    with pytest.raises(DimensionValueSourceMismatchError):
        service.add_value(dimension, code="X", name="Không thêm được")


def test_validate_value_source_reads_the_registry_it_is_given() -> None:
    """Registry tiêm vào phải là registry được dùng — không phải biến toàn cục."""
    catalogs = CatalogRegistry(PermissionRegistry())
    catalogs.register(CatalogSpec(slug="chi_co_o_day", model=CostObject, title="Chỉ có ở đây"))

    validate_value_source(DimensionValueSource.MASTER, "chi_co_o_day", catalogs=catalogs)

    with pytest.raises(DimensionValueSourceMismatchError):
        # Có thật trong registry của tiến trình, nhưng không có trong cái được
        # truyền vào — chứng minh hàm đọc đúng registry được tiêm.
        validate_value_source(DimensionValueSource.MASTER, "warehouses", catalogs=catalogs)


def test_the_seed_path_really_runs_the_shared_source_rule(
    owner_engine: Engine, dataset_alpha: DatasetRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Đường gieo mầm phải **thật sự đi qua** `validate_value_source`.

    Bản đầu của test này chỉ gọi thẳng hàm luật với một registry tiêm vào —
    không dòng nào chạm `seed.py`, nên xóa lời gọi khỏi đó vẫn xanh (sửa sau
    review M-4). Nay chặn chính hàm luật và khẳng định nó **được gọi**, với đúng
    tham số mà chiều dựng sẵn mang.

    Lý do phải canh: `declare` (ORM, đường API) và `ensure_builtin_dimensions`
    (Core, đường gieo mầm) ghi cùng một bảng qua hai đường khác nhau, và hai
    đường ghi mà mỗi đường một bộ luật là cách bộ luật lỏng hơn trở thành bộ
    luật thật.
    """
    calls: list[tuple[DimensionValueSource, str | None]] = []
    original = seed_module.validate_value_source

    def spy(value_source: DimensionValueSource, master_slug: str | None, **kwargs: object) -> None:
        calls.append((value_source, master_slug))
        original(value_source, master_slug, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(seed_module, "validate_value_source", spy)

    with owner_engine.begin() as connection:
        ensure_builtin_dimensions(connection, dataset_alpha.schema_name)

    assert calls == [(DimensionValueSource.LIST, None)]


def test_an_inactive_value_cannot_be_selected(session: Session) -> None:
    """Kiểm **cả hai cấp**: chiều đã tắt và giá trị đã tắt.

    Tắt một chiều mà giá trị con vẫn chọn được thì chứng từ tiếp tục mang giá
    trị của một chiều không còn ai đọc.
    """
    service = DimensionService(session)
    dimension = service.declare(code=_unique("L", session), name="Chiều L")
    value = service.add_value(dimension, code="V", name="Giá trị")
    service.ensure_selectable(value)

    value.is_active = False
    session.flush()
    with pytest.raises(DimensionValueNotFoundError):
        service.ensure_selectable(value)

    value.is_active = True
    dimension.is_active = False
    session.flush()
    with pytest.raises(DimensionValueNotFoundError):
        service.ensure_selectable(value)
