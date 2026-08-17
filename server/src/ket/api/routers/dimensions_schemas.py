"""Hình dạng request/response của chiều phân tích mở rộng (LD-08, FR-SYS-051)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ket.kernel.dimensions.models import (
    ACCOUNT_PREFIX_MAX_LENGTH,
    CODE_MAX_LENGTH,
    NAME_MAX_LENGTH,
    DimensionValueSource,
)

AccountPrefix = Annotated[str, Field(min_length=1, max_length=ACCOUNT_PREFIX_MAX_LENGTH)]
"""Một tiền tố số hiệu tài khoản. Trần bằng đúng độ rộng cột — nếu không, một
chuỗi dài hơn sẽ đi qua Pydantic rồi mới đổ ở `INSERT`, biến một lỗi nhập liệu
đơn giản thành lỗi 500."""


class DimensionResponse(BaseModel):
    """Định nghĩa một chiều."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    name_en: str | None
    value_source: DimensionValueSource
    master_slug: str | None
    is_required: bool
    applies_to_accounts: list[str] | None
    is_active: bool
    row_version: int


class DimensionListResponse(BaseModel):
    items: list[DimensionResponse]


class DimensionValueResponse(BaseModel):
    """Một giá trị của chiều `list`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    dimension_id: int
    code: str
    name: str
    name_en: str | None
    parent_id: int | None
    path: str
    level: int
    is_active: bool
    row_version: int


class DimensionValueListResponse(BaseModel):
    items: list[DimensionValueResponse]


class DimensionDeclareRequest(BaseModel):
    """Khai một chiều mới — thao tác **cấu hình**, không phải nhập liệu hằng ngày.

    Đây là đường mà tiêu chí "chiều mở rộng khai bằng cấu hình, không sửa code"
    đi qua ở v1. Màn hình cho người dùng cuối hoãn tới v1.1 (RT-20), nên ở v1
    người gọi là công cụ quản trị và gói cấu hình phase 5.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    name_en: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    value_source: DimensionValueSource = DimensionValueSource.LIST
    master_slug: str | None = Field(default=None, max_length=CODE_MAX_LENGTH)
    is_required: bool = False
    applies_to_accounts: list[AccountPrefix] | None = Field(
        default=None, max_length=200, description="Tiền tố số hiệu tài khoản; bỏ trống = mọi TK"
    )
    """Trần 200 phần tử: một chiều áp cho hai trăm dải tài khoản khác nhau gần
    như chắc chắn là dữ liệu sai, và không có trần thì một mảng khổng lồ đi
    thẳng vào cột `TEXT[]` mà không gì chặn."""


class DimensionValueAddRequest(BaseModel):
    """Thêm một giá trị vào chiều `list`."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    name_en: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    parent_id: int | None = None
