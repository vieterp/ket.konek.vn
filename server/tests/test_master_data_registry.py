"""Sổ đăng ký danh mục là nguồn duy nhất — và đây là bộ canh nó (lát 3B-1, H47).

Cơ chế "thêm danh mục = thêm model + spec, không thêm router" chỉ đúng chừng nào
**không có đường nào** thêm được một nửa. Bốn nửa có thể xảy ra, và cả bốn đều
hỏng im lặng:

1. Thêm model, quên đăng ký → bảng có, API không có. Vô hại nhưng phí.
2. Thêm model, quên đăng ký, rồi phase sau **tự viết một router riêng** cho nó →
   endpoint chạy mà không qua mã quyền nào.
3. Thêm cột riêng vào model, quên khai `extra_fields` → cột không ai đặt được.
4. Khai `extra_fields` một trường không có cột → API nhận giá trị rồi vứt đi, và
   màn hình hiện đúng thứ vừa nhập cho tới lần tải lại đầu tiên.

Test ở đây bắt cả bốn bằng cách đối chiếu ba nguồn với nhau: model đã ánh xạ,
registry, và bảng route thật của ứng dụng đã dựng.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import Boolean, inspect

from ket.api.idempotency import iter_api_routes
from ket.api.routers.master_data import PREFIX
from ket.api.routers.master_data_schemas import MasterDataBaseResponse, build_schemas
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.registry import REGISTRY, CatalogRegistry, CatalogSpec
from ket.kernel.security.permissions import (
    CATALOG_ACTIONS,
    MASTER_MODULE,
    Action,
    PermissionRegistry,
    permission_code,
)
from ket.main import create_app
from ket.model_registry import DatasetBase
from ket.settings import Settings

SHARED_COLUMNS = frozenset(
    {
        "id",
        "uid",
        "code",
        "name",
        "name_en",
        "parent_id",
        "path",
        "level",
        "is_group",
        "is_active",
        "branch_id",
        "row_version",
    }
)
"""Cột mà `MasterDataRow` cấp cho mọi danh mục.

Khẳng định **bằng giá trị** chứ không tính lại từ chính lớp gốc: tính lại thì
test đồng ý với bất cứ thứ gì lớp gốc trở thành, kể cả khi ai đó vô tình xóa
`row_version` khỏi nó. `test_shared_columns_match_the_base_class` giữ hằng này
trung thực theo chiều ngược lại."""


def _master_data_models() -> set[type[MasterDataRow]]:
    return {
        mapper.class_
        for mapper in DatasetBase.registry.mappers
        if issubclass(mapper.class_, MasterDataRow)
    }


def _mapped_columns(model: type[MasterDataRow]) -> set[str]:
    return {column.key for column in DatasetBase.metadata.tables[model.__tablename__].columns}


def test_shared_columns_match_the_base_class() -> None:
    """Hằng `SHARED_COLUMNS` phải đúng bằng cột của `MasterDataRow`.

    Nếu lớp gốc thêm hoặc bớt cột mà không ai sửa hằng này, mọi khẳng định về
    "cột riêng" bên dưới sẽ lệch một trường — và lệch theo hướng **im lặng**,
    vì phép trừ tập hợp vẫn cho ra một kết quả trông hợp lý.
    """
    assert _mapped_columns(CostObject) == SHARED_COLUMNS


def test_every_master_data_model_is_registered() -> None:
    """Model có bảng nhưng không có trong registry = danh mục không ai gọi được.

    Đây là cổng cho trường hợp 1 và 2 ở docstring module. Sửa bằng cách thêm một
    `CatalogSpec` vào `_register_all`, **không** bằng cách nới test này.
    """
    registered = {spec.model for spec in REGISTRY.specs()}
    missing = {model.__name__ for model in _master_data_models() - registered}

    assert not missing, (
        f"Model kế thừa MasterDataRow nhưng chưa đăng ký: {sorted(missing)}. "
        "Thêm CatalogSpec vào ket/kernel/master_data/registry.py."
    )


def test_slug_and_table_name_stay_distinct_concepts() -> None:
    """`slug` khai tường minh, không suy từ tên bảng (H49).

    Không có gì bắt hai thứ phải khác nhau — hiện tại mười bảy danh mục đặt
    chúng bằng nhau vì đó là lựa chọn dễ đọc nhất. Cái test này canh là chúng
    **được phép** khác: nếu một ngày `CatalogSpec` đổi sang tự suy `slug` từ
    `__tablename__` thì đường dẫn công khai sẽ đi theo mỗi lần đổi tên bảng.
    """
    spec = CatalogSpec(slug="mot_slug_khac", model=CostObject, title="Thử")
    assert spec.slug != spec.model.__tablename__
    assert spec.entity_type == "cost_objects"


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_extra_fields_match_the_mapped_columns_both_ways(spec: CatalogSpec) -> None:
    """`extra_fields` phải khai **đúng** tập cột riêng — không thừa, không thiếu.

    Hai chiều, vì hai hướng lệch cho hai kiểu lỗi khác nhau (trường hợp 3 và 4 ở
    docstring module), và một test chỉ kiểm một chiều sẽ để lọt kiểu còn lại.
    """
    columns = _mapped_columns(spec.model) - SHARED_COLUMNS
    declared = set(spec.extra_fields.model_fields) if spec.extra_fields else set()

    assert declared == columns, (
        f"{spec.slug}: `extra_fields` khai {sorted(declared)} còn bảng có {sorted(columns)}. "
        "Cột thiếu thì không ai đặt được; trường thừa thì API nhận rồi vứt đi."
    )


def test_at_least_one_catalog_exercises_the_create_only_mechanism() -> None:
    """Cổng bên dưới chạy thật ở ít nhất một danh mục (review L-2).

    Nó `return` ngay với danh mục không khai `extra_update_fields` — hôm nay là
    19/20. Không có dòng này thì ngày ai đó bỏ khai báo khỏi `items`, cổng ấy
    lặng lẽ trở về 20/20 rỗng: xanh vì không đo gì, không vì đo được điều gì.
    """
    assert any(spec.extra_update_fields is not None for spec in REGISTRY.specs()), (
        "Không danh mục nào khai `extra_update_fields` — "
        "`test_create_only_fields_are_declared_by_inheritance` đang rỗng."
    )


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_create_only_fields_are_declared_by_inheritance(spec: CatalogSpec) -> None:
    """Tập cột sửa được phải là **lớp cha** của tập cột tạo mới (H69).

    Quan hệ kế thừa là cách khai "trường này chỉ đặt lúc tạo" ở registry, và nó
    được chọn thay cho một danh sách tên trường vì nó mang theo hai thứ mà danh
    sách tên không mang: model tạo mới thừa hưởng trọn **validator** của model
    sửa (không luật nào chỉ áp cho một đường), và tên cột không phải gõ lại bằng
    chuỗi ở chỗ thứ hai.

    Đảo ngược quan hệ ấy là kiểu lỗi test này tồn tại để bắt: `extra_update_fields`
    rộng hơn `extra_fields` nghĩa là đường **sửa** nhận một trường mà đường tạo
    không có — `extra_values` sẽ không bao giờ chuyển nó xuống dịch vụ vì nó đọc
    danh sách trường từ `extra_fields`, nên giá trị người dùng nhập biến mất im
    lặng.
    """
    if spec.extra_update_fields is None:
        return

    assert spec.extra_fields is not None, (
        f"{spec.slug}: khai `extra_update_fields` mà không có `extra_fields` — "
        "tập cột sửa được chỉ có nghĩa khi có tập cột riêng để hẹp hơn."
    )
    assert issubclass(spec.extra_fields, spec.extra_update_fields), (
        f"{spec.slug}: `extra_fields` phải là lớp con của `extra_update_fields`. "
        "Trường chốt một lần lúc tạo khai bằng cách **thêm** vào lớp con, "
        "không phải bằng hai model rời."
    )
    create_only = set(spec.extra_fields.model_fields) - set(spec.extra_update_fields.model_fields)
    assert create_only, (
        f"{spec.slug}: hai model có cùng tập trường nên `extra_update_fields` không "
        "nói thêm điều gì — bỏ nó đi thay vì để một khai báo không có tác dụng."
    )
    update_fields = set(build_schemas(spec).update.model_fields)
    assert not (create_only & update_fields), (
        f"{spec.slug}: trường chốt một lần {sorted(create_only & update_fields)} vẫn "
        "xuất hiện trong thân request sửa."
    )


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_response_model_carries_every_column(spec: CatalogSpec) -> None:
    """Model phản hồi = bộ chung + cột riêng, không thiếu trường nào.

    Trường hay bị quên nhất là `row_version` — thiếu nó thì màn hình không gửi
    lại được phiên bản và khóa lạc quan (FR-NFR-005) mất tác dụng trên đúng danh
    mục đó, trong khi mọi thứ khác vẫn chạy.
    """
    response_fields = set(build_schemas(spec).response.model_fields)

    assert response_fields == _mapped_columns(spec.model)
    assert "row_version" in response_fields


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_every_catalog_has_its_four_permission_codes(spec: CatalogSpec) -> None:
    """Đăng ký danh mục **là** đăng ký quyền — bốn hành vi của một danh mục.

    Không `post`/`unpost`/`print`: danh mục không ghi sổ và không in chứng từ,
    nên những ô đó trên màn hình phân quyền sẽ không bao giờ có tác dụng.
    """
    from ket.kernel.security.permissions import REGISTRY as PERMISSIONS

    codes = set(PERMISSIONS.codes())
    for action in CATALOG_ACTIONS:
        assert spec.permission_code(action) in codes

    for action in set(Action) - CATALOG_ACTIONS:
        assert permission_code(MASTER_MODULE, spec.slug, action) not in codes


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_every_catalog_gets_the_same_six_routes(spec: CatalogSpec) -> None:
    """Sáu thao tác cho **mọi** danh mục, không danh mục nào thiếu cái nào.

    Đọc bảng route của ứng dụng đã dựng chứ không đọc mã nguồn router: đây là
    thứ người dùng gọi được, và nó là bằng chứng duy nhất rằng vòng lặp gắn
    route thật sự chạy hết chứ không dừng ở danh mục thứ ba.
    """
    app = create_app(Settings(verify_schema_on_startup=False))
    paths = {
        (route.path, method)
        for route in iter_api_routes(app.routes)
        for method in route.methods
        if isinstance(route, APIRoute)
    }

    assert (f"{PREFIX}/{spec.slug}", "GET") in paths
    assert (f"{PREFIX}/{spec.slug}", "POST") in paths
    assert (f"{PREFIX}/{spec.slug}/{{record_id}}", "GET") in paths
    assert (f"{PREFIX}/{spec.slug}/{{record_id}}", "PUT") in paths
    assert (f"{PREFIX}/{spec.slug}/{{record_id}}/parent", "PUT") in paths
    assert (f"{PREFIX}/{spec.slug}/{{record_id}}", "DELETE") in paths
    # Lát 3B-2 thêm thao tác thứ bảy — gộp bản ghi (FR-SYS-016). Cũng sinh cho
    # **mọi** danh mục, cùng lý do: danh mục thứ hai mươi không được là danh mục
    # duy nhất không gộp được.
    assert (f"{PREFIX}/{spec.slug}/actions/merge", "POST") in paths


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_flags_point_at_real_boolean_columns(spec: CatalogSpec) -> None:
    """Bộ lọc `?flag=` phải trỏ vào một cột boolean có thật của chính danh mục.

    Sai tên cột thì lỗi chỉ nổ ra ở request đầu tiên có người lọc — mà đường lọc
    là đường mà giao diện dùng để dựng danh sách khách hàng, tức lỗi nổ ở màn
    hình chính chứ không ở CI.
    """
    columns = inspect(spec.model).columns
    values = [flag.value for flag in spec.flags]

    assert len(values) == len(set(values)), f"{spec.slug}: hai bộ lọc trùng giá trị `?flag=`"
    for flag in spec.flags:
        assert flag.column in columns, f"{spec.slug}: cột {flag.column!r} không có trong bảng"
        assert isinstance(columns[flag.column].type, Boolean), (
            f"{spec.slug}: {flag.column!r} không phải cột boolean nên `?flag=` sẽ lọc sai"
        )


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.slug)
def test_references_point_at_registered_catalogs(spec: CatalogSpec) -> None:
    """Khóa ngoại khai trong `references` phải là cột thật **và** trỏ danh mục có đăng ký.

    Danh mục đích phải có trong registry vì phép kiểm phạm vi (`ensure_catalog_choice`)
    tra nó ở đó: một `slug` gõ sai sẽ biến phép kiểm thành `404` cho **mọi** giá
    trị hợp lệ, tức người dùng không gán được điều khoản thanh toán nào cả.
    """
    columns = inspect(spec.model).columns

    for reference in spec.references:
        assert reference.field in columns, f"{spec.slug}: {reference.field!r} không phải cột"
        assert columns[reference.field].foreign_keys, (
            f"{spec.slug}: {reference.field!r} khai là tham chiếu nhưng không có khóa ngoại"
        )
        target = REGISTRY.get(reference.slug)
        assert target is not None, f"{spec.slug}: danh mục đích {reference.slug!r} chưa đăng ký"
        referenced_tables = {key.column.table.name for key in columns[reference.field].foreign_keys}
        assert target.entity_type in referenced_tables, (
            f"{spec.slug}: {reference.field!r} trỏ tới {referenced_tables} "
            f"nhưng khai là {target.entity_type}"
        )


def test_registering_a_catalog_also_registers_its_permission_type() -> None:
    """Hai việc trong một lời gọi — chứng minh trên registry **tách riêng**.

    Registry riêng chứ không registry thật: `PermissionRegistry.register` ném khi
    trùng khóa, nên một test ghi vào registry của tiến trình sẽ hoặc nổ, hoặc
    làm bẩn nó cho mọi test chạy sau.
    """
    permissions = PermissionRegistry()
    catalogs = CatalogRegistry(permissions)

    catalogs.register(CatalogSpec(slug="thu_nghiem", model=CostObject, title="Thử nghiệm"))

    assert set(permissions.codes()) == {
        f"{MASTER_MODULE}.thu_nghiem.{action.value}" for action in CATALOG_ACTIONS
    }


def test_registering_the_same_slug_twice_is_refused() -> None:
    """Ghi đè im lặng làm danh mục thua cuộc biến mất theo thứ tự import.

    Khẳng định **thông điệp** chứ không chỉ loại ngoại lệ (sửa sau review L-3):
    `PermissionRegistry.register` cũng ném `ValueError` cho khóa trùng và nó
    chạy **trước**, nên một test chỉ hỏi `pytest.raises(ValueError)` sẽ xanh kể
    cả khi nhánh chặn trong `CatalogRegistry.register` bị gỡ hẳn.
    """
    catalogs = CatalogRegistry(PermissionRegistry())
    spec = CatalogSpec(slug="thu_nghiem", model=CostObject, title="Thử nghiệm")
    catalogs.register(spec)

    with pytest.raises(ValueError, match=r"Danh mục 'thu_nghiem' đã được đăng ký"):
        catalogs.register(spec)


def test_the_catalog_guard_fires_before_the_permission_guard() -> None:
    """Nhánh chặn trùng `slug` phải tự nó chặn được, không nhờ registry quyền.

    Dựng một registry quyền **đã có sẵn** loại quyền tương ứng: nếu
    `CatalogRegistry` không tự kiểm, lời gọi thứ hai sẽ đi tới
    `PermissionRegistry.register` và ném ở đó — cùng loại ngoại lệ, khác nguyên
    nhân. Ở đây lời gọi **thứ nhất** phải hỏng vì quyền, còn danh mục thì chưa
    được ghi vào — chứng minh thứ tự trong `register` đúng như docstring nói.
    """
    permissions = PermissionRegistry()
    catalogs = CatalogRegistry(permissions)
    spec = CatalogSpec(slug="thu_nghiem", model=CostObject, title="Thử nghiệm")
    permissions.register(spec.document_type())

    with pytest.raises(ValueError, match=r"Loại chứng từ master\.thu_nghiem đã được đăng ký"):
        catalogs.register(spec)

    # Đăng ký quyền hỏng thì danh mục **không** được giữ lại nửa vời.
    assert catalogs.get("thu_nghiem") is None


@pytest.mark.parametrize(
    "slug", ["co-gach-ngang", "CoChuHoa", "có_dấu", "", "co.dau.cham", "co khoang trang"]
)
def test_slug_must_be_a_plain_identifier(slug: str) -> None:
    """`slug` là **một đoạn của mã quyền**, mà mã quyền tách bằng dấu chấm.

    Dấu chấm lọt vào là mọi phép tách mã sau này lệch một đoạn; ký tự Unicode
    lọt vào là một đường dẫn phải mã hóa URL. Chặn ở lúc dựng `CatalogSpec`, nơi
    nó nổ ngay khi nạp module chứ không ở request đầu tiên.
    """
    with pytest.raises(ValueError):
        CatalogSpec(slug=slug, model=CostObject, title="Thử")


def test_specs_are_sorted_so_the_openapi_spec_is_stable() -> None:
    """Thứ tự đăng ký không được rò vào đặc tả OpenAPI đã commit.

    Không sắp thì `test_openapi_contract` đỏ ngẫu nhiên theo thứ tự import —
    kiểu đỏ mà người gặp sẽ chạy lại CI thay vì đọc.
    """
    slugs = [spec.slug for spec in REGISTRY.specs()]
    assert slugs == sorted(slugs)


def test_base_response_declares_uid_as_a_string() -> None:
    """`uid` ra ngoài dưới dạng chuỗi (RT-19).

    Client chỉ chuyền tiếp nó, không tính toán trên nó; một kiểu chuỗi thì mọi
    tầng đọc y hệt nhau và không tầng nào phải biết đó là UUIDv7.
    """
    assert MasterDataBaseResponse.model_fields["uid"].annotation is str
