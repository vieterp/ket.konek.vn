"""Endpoint tùy chọn hai cấp (`/api/v1/system/settings`) — FR-SYS-060, BR-SYS-06.

Tách khỏi `routers/system.py` thay vì nối thêm vào đó: tệp kia đã lo bốn nhóm
việc, và tùy chọn có luật phân quyền **riêng** — quy tắc quan trọng nhất của
nhóm này (ai sửa được cấp nào) sẽ chìm nếu nằm lẫn giữa gán vai trò và đọc nhật
ký.

Luật đó, nói một lần cho gọn:

* đọc: `system.setting.view` — màn hình nào cũng cần biết ngôn ngữ và cách làm
  tròn, nên đây gần như là quyền của mọi người dùng;
* ghi cấp **user**: cũng chỉ cần `system.setting.view`, và chỉ ghi được cho
  **chính mình** — thói quen bàn phím của một người không phải việc của người
  khác, kể cả quản trị viên;
* ghi cấp **system**: `system.setting.edit` — nó đổi cách ghi sổ của cả doanh
  nghiệp.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.system_settings_schemas import (
    SettingListResponse,
    SettingResponse,
    SettingUpdateRequest,
)
from ket.kernel.config import settings_service
from ket.kernel.config.catalog import CATALOG, SettingScope
from ket.kernel.config.settings_service import EffectiveSetting
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code

router = APIRouter(prefix="/api/v1/system/settings", tags=["system"])

SETTING_VIEW = permission_code(SYSTEM_MODULE, "setting", Action.VIEW)
SETTING_EDIT = permission_code(SYSTEM_MODULE, "setting", Action.EDIT)

SettingReader = Annotated[AuthorizedRequest, Depends(require_permission(SETTING_VIEW))]


def _response(setting: EffectiveSetting) -> SettingResponse:
    definition = CATALOG[setting.key]
    return SettingResponse(
        key=setting.key,
        value=setting.raw_value,
        value_type=setting.value_type,
        source=setting.source,
        system_row_version=setting.system_row_version,
        user_row_version=setting.user_row_version,
        scopes=sorted(scope.value for scope in definition.scopes),
        description=definition.description,
    )


@router.get("", response_model=SettingListResponse)
def list_settings(authorized: SettingReader, factory: SessionFactory) -> SettingListResponse:
    """Toàn bộ danh mục kèm giá trị **đang có hiệu lực** cho người gọi.

    Trả cả khóa chưa ai cấu hình (nguồn `default`): màn hình thiết lập phải dựng
    được từ đúng một lượt gọi, và một danh sách chỉ có giá trị đã lưu sẽ buộc
    client mang theo bản sao của catalog — bản sao sẽ lệch ngay ở phase sau.
    """
    with unit_of_work(factory, authorized.scope) as session:
        values = settings_service.effective_settings(session, user_id=authorized.scope.user_id)
        return SettingListResponse(items=[_response(value) for value in values])


@router.put("/{key}", response_model=SettingResponse)
def update_setting(
    key: str,
    payload: SettingUpdateRequest,
    authorized: SettingReader,
    factory: SessionFactory,
) -> SettingResponse:
    """Ghi một tùy chọn ở một cấp, có kiểm phiên bản (FR-NFR-005).

    Quyền kiểm **trong thân hàm** chứ không bằng dependency, vì nó phụ thuộc
    `scope` nằm trong thân request: `require_permission` chạy trước khi thân
    request được phân giải. Dependency ở chữ ký vẫn giữ mức sàn (`view`), nên
    người không có quyền gì dừng lại từ trước đó.
    """
    if payload.scope is SettingScope.SYSTEM:
        authorized.access.require(SETTING_EDIT)

    with unit_of_work(factory, authorized.scope) as session:
        value = settings_service.set_setting(
            session,
            key=key,
            scope=payload.scope,
            # Cấp user luôn ghi cho **chính người gọi**: không nhận `user_id` từ
            # thân request, để không có đường nào sửa tùy chọn của người khác —
            # kể cả cho quản trị viên, vì đó không phải việc cần làm hộ ai.
            user_id=authorized.scope.user_id,
            raw_value=payload.value,
            expected_row_version=payload.row_version,
        )
        return _response(value)
