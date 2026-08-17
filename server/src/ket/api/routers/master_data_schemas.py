"""Hình dạng request/response của danh mục — **sinh từ registry**, không gõ tay.

Mỗi danh mục nhận bốn model riêng (`…Response`, `…ListResponse`,
`…CreateRequest`, `…UpdateRequest`) dựng bằng `pydantic.create_model` từ
`CatalogSpec`. Mười một danh mục thuần cây nhận đúng bộ cột chung; sáu danh mục
có `extra_fields` nhận thêm phần riêng của mình.

Vì sao model **riêng cho từng danh mục** thay vì một `MasterDataResponse` chung
mang trường `attributes` kiểu union (quyết định H47): union buộc client thu hẹp
kiểu mỗi lần đọc một danh mục có cột riêng — tức phần hay dùng nhất của API lại
là phần khó dùng nhất. Với model riêng, `GET /api/v1/master/banks` sinh ra type
TypeScript có sẵn `short_name` và `swift_code`.

Vì sao `create_model` thay vì mười bảy lần chép tay: chép tay thì danh mục thứ
mười hai quên trường `row_version`, và cái giá là một màn hình mất khóa lạc quan
(FR-NFR-005) mà không có gì báo. Chỗ duy nhất được phép khác nhau giữa các danh
mục là `extra_fields` — khai cạnh chính model ORM, nơi người thêm cột nhìn thấy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import GenericAlias
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, create_model

from ket.kernel.master_data.base import CODE_MAX_LENGTH, NAME_MAX_LENGTH
from ket.kernel.master_data.registry import CatalogSpec

CodeField = Annotated[str, Field(min_length=1, max_length=CODE_MAX_LENGTH)]
NameField = Annotated[str, Field(min_length=1, max_length=NAME_MAX_LENGTH)]
OptionalNameField = Annotated[str | None, Field(default=None, max_length=NAME_MAX_LENGTH)]


class MasterDataBaseResponse(BaseModel):
    """Bộ cột chung mà **mọi** danh mục trả về (`MasterDataRow`)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: str
    """Chuỗi chứ không `UUID`: `uid` là **định danh để nối dữ liệu** (RT-19) —
    client chỉ chuyền tiếp chứ không tính toán trên nó, và một chuỗi thì mọi
    tầng đọc y hệt nhau."""

    code: str
    name: str
    name_en: str | None
    parent_id: int | None
    path: str
    level: int
    is_group: bool
    is_active: bool
    branch_id: int | None
    row_version: int


class MasterDataBaseCreateRequest(BaseModel):
    """Bộ trường chung để tạo một bản ghi danh mục.

    `extra="forbid"`: một trường gõ sai tên bị từ chối ngay thay vì bị bỏ qua.
    Bỏ qua im lặng nghĩa là người dùng thấy form lưu thành công còn giá trị vừa
    nhập thì biến mất — kiểu lỗi tốn nhiều giờ hỗ trợ nhất.
    """

    model_config = ConfigDict(extra="forbid")

    code: CodeField
    name: NameField
    name_en: OptionalNameField
    parent_id: int | None = None
    is_group: bool = False
    branch_id: int | None = None
    """`None` = danh mục dùng chung toàn công ty (FR-SYS-018)."""


class MasterDataBaseUpdateRequest(BaseModel):
    """Sửa một bản ghi, có kiểm phiên bản (FR-NFR-005).

    `row_version` **bắt buộc**, không có mặc định — cùng lập luận đã ghi ở
    `BranchUpdateRequest`: một trường tùy chọn ở đây nghĩa là client nào quên
    gửi sẽ ghi đè im lặng, tức đúng cái mà khóa lạc quan sinh ra để chặn.

    Đổi **cha** không nằm ở đây mà ở endpoint riêng: nó chạm cả nhánh con bằng
    một câu `UPDATE` và có thể bị từ chối vì tạo chu trình — hệ quả khác hẳn một
    lần sửa tên, nên nó xứng đáng là một thao tác client gọi có chủ đích.
    """

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    code: CodeField
    name: NameField
    name_en: OptionalNameField
    is_active: bool


class MasterDataMoveRequest(BaseModel):
    """Chuyển một nút (và cả nhánh dưới nó) sang nhóm cha khác (FR-SYS-011)."""

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    new_parent_id: int | None = None
    """`None` = đưa nút lên làm nút gốc."""


@dataclass(frozen=True)
class CatalogSchemas:
    """Bốn model của một danh mục, dựng một lần lúc nạp module."""

    response: type[BaseModel]
    list_response: type[BaseModel]
    create: type[BaseModel]
    update: type[BaseModel]


def _class_prefix(slug: str) -> str:
    """`units_of_measure` → `UnitsOfMeasure`.

    Tên lớp đi thẳng vào `components.schemas` của OpenAPI, tức vào tên type mà
    client sinh ra và lập trình viên client gõ. Sinh từ `slug` chứ không từ tên
    lớp ORM: `slug` là hợp đồng công khai (H49), tên lớp ORM thì không —
    `DocumentTypeCatalog` là một cái tên chỉ có nghĩa bên trong máy chủ.
    """
    return "".join(part.capitalize() for part in slug.split("_"))


def build_schemas(spec: CatalogSpec) -> CatalogSchemas:
    """Dựng bộ model cho một danh mục.

    Cột riêng vào bằng **kế thừa** (`__base__`) chứ không bằng cách sao chép
    từng trường: kế thừa mang theo cả validator của model đó — ví dụ luật "cửa
    sổ chiết khấu phải nằm trong hạn nợ" của `PaymentTermFields`. Sao chép
    trường sẽ để lại luật đó phía sau và không có gì báo, vì model vẫn dựng
    được và vẫn nhận đúng các trường.
    """
    prefix = _class_prefix(spec.slug)
    extra = spec.extra_fields

    response: type[BaseModel] = create_model(
        f"{prefix}Response",
        __base__=(MasterDataBaseResponse, extra) if extra else MasterDataBaseResponse,
        __doc__=f"{spec.title} — một bản ghi.",
    )
    # `GenericAlias(list, (response,))` thay vì viết `list[response]`: cả hai cho
    # ra **cùng một** đối tượng lúc chạy, nhưng dạng viết tay là cú pháp *kiểu*
    # đặt trên một biến, nên mypy đòi `response` phải là tên kiểu tĩnh — mà nó là
    # lớp dựng lúc chạy. Gọi hàm dựng thẳng thì đây là một biểu thức bình thường,
    # không cần khai miễn trừ kiểm kiểu nào — ADR-015 đặt ngưỡng miễn trừ ở **0**
    # và có cổng CI đếm.
    item_list = GenericAlias(list, (response,))
    list_response: type[BaseModel] = create_model(
        f"{prefix}ListResponse",
        __doc__=f"{spec.title} — một trang bản ghi.",
        items=(item_list, ...),
        # Tổng **trước** khi cắt trang — xem `MasterDataService.Page`.
        total=(int, ...),
    )
    create_request: type[BaseModel] = create_model(
        f"{prefix}CreateRequest",
        __base__=(MasterDataBaseCreateRequest, extra) if extra else MasterDataBaseCreateRequest,
        __doc__=f"{spec.title} — tạo mới.",
    )
    update_request: type[BaseModel] = create_model(
        f"{prefix}UpdateRequest",
        __base__=(MasterDataBaseUpdateRequest, extra) if extra else MasterDataBaseUpdateRequest,
        __doc__=f"{spec.title} — sửa.",
    )
    return CatalogSchemas(
        response=response,
        list_response=list_response,
        create=create_request,
        update=update_request,
    )


def extra_values(spec: CatalogSpec, payload: BaseModel) -> dict[str, object]:
    """Phần cột riêng trong một thân request, tách khỏi bộ trường chung.

    Đọc danh sách trường từ `spec.extra_fields` chứ không lấy hiệu của hai tập
    trường: hiệu tập sẽ **im lặng đổi nghĩa** nếu một ngày nào đó bộ cột chung
    thêm trường — cột riêng của danh mục bỗng thiếu một cái mà không ai sửa gì.
    """
    if spec.extra_fields is None:
        return {}
    return {field: getattr(payload, field) for field in spec.extra_fields.model_fields}
