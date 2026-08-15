"""Hợp đồng JSON của nhóm `/api/v1/system/settings` (ADR-015)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ket.kernel.config.catalog import SettingScope


class SettingResponse(BaseModel):
    """Một tùy chọn với giá trị đang có hiệu lực cho người gọi."""

    key: str
    value: str
    """Dạng **chuỗi**, đúng như trong DB. Client ép kiểu theo `value_type`.

    Không trả giá trị đã ép kiểu sẵn: một trường JSON đổi kiểu theo giá trị của
    trường khác là thứ type sinh từ OpenAPI không diễn tả được, và client sẽ
    phải `any` đúng chỗ đáng ra cần chặt nhất."""

    value_type: str
    source: str
    """`default` | `system` | `user` — giá trị này từ đâu ra."""

    system_row_version: int | None
    user_row_version: int | None
    """Phiên bản của **từng cấp** — client sửa cấp nào thì gửi lại phiên bản của
    cấp đó (`null` nghĩa là cấp đó chưa có dòng nào).

    Hai trường chứ không phải một "phiên bản của giá trị đang hiệu lực": xem
    `settings_service.EffectiveSetting` để biết một trường duy nhất hỏng thế
    nào — nó vừa chặn người dùng đặt giá trị riêng, vừa cho phép ghi đè im lặng
    giá trị chung."""

    scopes: list[str]
    description: str


class SettingListResponse(BaseModel):
    items: list[SettingResponse]


class SettingUpdateRequest(BaseModel):
    """Ghi một tùy chọn ở đúng một cấp.

    `row_version` là phiên bản của dòng **ở chính cấp `scope`** — không phải
    của giá trị đang hiệu lực. Khai tường minh cả khi bằng `null`: "tôi tin
    rằng chưa ai ghi khóa này ở cấp này" cũng là một khẳng định về hiện trạng,
    và nó sai được y như một số phiên bản cũ.
    """

    scope: SettingScope
    value: str = Field(max_length=1000)
    row_version: int | None = Field(default=None, ge=1)
