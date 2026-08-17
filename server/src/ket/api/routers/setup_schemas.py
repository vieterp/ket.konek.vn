"""Hình dạng phản hồi của màn hình Thiết lập (nguyên tắc U14)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SettingSource = Literal["setting", "fiscal_year"]

ANYTIME_GROUP = "anytime"
DECIDED_ONCE_GROUP = "decided_once"


class SetupItem(BaseModel):
    """Một quyết định thiết lập, dù nó đến từ bảng tùy chọn hay từ năm tài chính."""

    key: str
    """`money.scale`, hoặc `fiscal_years.2026.accounting_scheme`.

    Có tiền tố nguồn để hai họ khóa không đụng nhau, và để màn hình biết gọi
    endpoint nào khi người dùng bấm sửa."""

    title: str
    value: str
    """Giá trị hiện tại ở dạng chuỗi — màn hình chỉ hiển thị, không tính toán
    trên nó (mọi phép tính tiền ở server, LD-03)."""

    source: SettingSource
    fiscal_year_code: str | None
    """Năm tài chính của quyết định, khi `source = fiscal_year`."""

    is_editable: bool
    locked_reason: str | None
    """Câu tiếng Việt nêu **vì sao** không sửa được, `None` khi sửa được."""


class SetupGroup(BaseModel):
    """Một nhóm quyết định, phân theo **hệ quả** chứ không theo màn hình."""

    key: str
    title: str
    description: str
    items: list[SetupItem]


class SettingsGroupsResponse(BaseModel):
    """Hai nhóm của màn hình Thiết lập: đổi bất kỳ lúc nào / chốt một lần."""

    groups: list[SetupGroup]
