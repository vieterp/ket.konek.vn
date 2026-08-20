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

from fastapi import APIRouter, Depends, File, UploadFile

from ket.api.dependencies import AppSettings, AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.system_settings_schemas import (
    ReportLogoResponse,
    SettingListResponse,
    SettingResponse,
    SettingUpdateRequest,
)
from ket.kernel.attachments import storage
from ket.kernel.config import settings_service
from ket.kernel.config.catalog import (
    CATALOG,
    REPORT_LOGO_HASH_KEY,
    REPORT_LOGO_MEDIA_KEY,
    SettingScope,
)
from ket.kernel.config.settings_service import EffectiveSetting
from ket.kernel.errors import (
    AttachmentStorageNotConfiguredError,
    SettingValueInvalidError,
)
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


@router.post("/logo", response_model=ReportLogoResponse)
def upload_report_logo(
    authorized: SettingReader,
    factory: SessionFactory,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> ReportLogoResponse:
    """Nút một-bước gán logo báo cáo (FR-RPT-010, lát 5E).

    Trước lát này, gán logo là hai việc tay: tải tệp lên kho rồi dán
    `content_hash` vào `report.logo_content_hash` — đường mà không kế toán nào
    tự đi được. Ở đây gộp lại: ghi blob vào kho content-addressed (KHÔNG tạo
    dòng `attachments` — logo là nhận diện đơn vị mọi chi nhánh cùng thấy, còn
    bảng attachments nằm sau RLS chi nhánh, xem quyết định 5D) rồi ghi cả hai
    khóa settings trong một transaction.

    Quyền `system.setting.edit` — đúng quyền của việc nó làm (đổi hai khóa cấp
    hệ thống). Không đòi idempotency key: gửi lại cùng tệp ghi lại cùng hash và
    cùng giá trị — không có trạng thái nào bị nhân đôi.
    """
    authorized.access.require(SETTING_EDIT)
    definition = CATALOG[REPORT_LOGO_MEDIA_KEY]
    media_type = file.content_type or ""
    if definition.choices is not None and media_type not in definition.choices:
        raise SettingValueInvalidError(
            "Kiểu tệp logo không được hỗ trợ",
            key=REPORT_LOGO_MEDIA_KEY,
            allowed=", ".join(sorted(definition.choices)),
            value=media_type or None,
        )
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp đính kèm (KET_ATTACHMENTS_DIR)"
        )
    # Ghi blob TRƯỚC khi mở transaction — cùng lý do với upload_attachment:
    # hash chỉ biết được sau khi đọc hết tệp.
    stored = storage.store_stream(
        settings.attachments_dir,
        authorized.scope.dataset_schema,
        file.file,
        max_bytes=settings.attachment_max_bytes,
    )
    with unit_of_work(factory, authorized.scope) as session:
        for key, raw_value in (
            (REPORT_LOGO_HASH_KEY, stored.content_hash),
            (REPORT_LOGO_MEDIA_KEY, media_type),
        ):
            current = next(
                item
                for item in settings_service.effective_settings(
                    session, user_id=authorized.scope.user_id
                )
                if item.key == key
            )
            settings_service.set_setting(
                session,
                key=key,
                scope=SettingScope.SYSTEM,
                user_id=authorized.scope.user_id,
                raw_value=raw_value,
                # Phiên bản đọc trong CÙNG transaction với lệnh ghi: nút
                # một-bước không có màn hình nào cầm sẵn row_version để gửi.
                expected_row_version=current.system_row_version,
            )
    return ReportLogoResponse(
        content_hash=stored.content_hash,
        media_type=media_type,
        byte_size=stored.byte_size,
    )
