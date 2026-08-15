"""Hợp đồng JSON của nhóm `/api/v1/system` — Pydantic ở mọi ranh giới (ADR-015).

Tách khỏi `system.py` theo đúng lý do đã tách `auth_schemas.py`: hình dạng phản
hồi là **hợp đồng với client** và với type sinh từ OpenAPI, nên nó phải đọc được
mà không phải lội qua mã điều phối.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DatasetSummary(BaseModel):
    """Một dữ liệu kế toán trong màn hình chọn (FR-SYS-001)."""

    code: str
    name: str
    scheme: str


class DatasetListResponse(BaseModel):
    items: list[DatasetSummary]


class AccessResponse(BaseModel):
    """Quyền hiệu lực của người đang gọi, **trong dataset của request**.

    Client dùng để ẩn menu và nút — không phải để quyết định cho phép: mọi lần
    kiểm thật vẫn nằm ở server (`require_permission`). Trả về đây chỉ để màn
    hình không mời người dùng bấm vào thứ chắc chắn bị từ chối.
    """

    dataset_code: str
    permissions: list[str]
    branch_ids: list[int]
    acting_branch_id: int | None


class BranchResponse(BaseModel):
    id: int
    code: str
    name: str
    name_en: str | None
    is_active: bool

    row_version: int
    """Client gửi lại giá trị này khi lưu (FR-NFR-005). Có trong **mọi** phản
    hồi của bảng có phiên bản, kể cả danh sách: form mở từ màn hình danh sách
    thì không có lượt `GET` chi tiết nào để lấy số này."""


class BranchListResponse(BaseModel):
    items: list[BranchResponse]


class BranchCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)


class BranchUpdateRequest(BaseModel):
    """Sửa chi nhánh. `code` không sửa được — nó là mã đối chiếu của cả sổ sách.

    `row_version` **bắt buộc**, không có mặc định: một trường tùy chọn ở đây
    nghĩa là client nào quên gửi sẽ ghi đè im lặng, tức là đúng cái mà khóa lạc
    quan sinh ra để chặn.
    """

    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    is_active: bool
    row_version: int = Field(ge=1)


class RoleGrantRequest(BaseModel):
    role_code: str = Field(min_length=1, max_length=50)


class BranchGrantRequest(BaseModel):
    branch_code: str = Field(min_length=1, max_length=50)


class GrantResponse(BaseModel):
    """`changed=False` nghĩa là đã có sẵn — gọi lại không phải lỗi.

    Idempotent có chủ đích: màn hình phân quyền là nơi người ta bấm hai lần vì
    không chắc lần đầu đã ăn chưa, và một lỗi `409` ở đó chỉ làm họ hoang mang.
    """

    changed: bool


class AuditEntryResponse(BaseModel):
    """Một dòng nhật ký nghiệp vụ (FR-NFR-013)."""

    id: int
    occurred_at: datetime
    user_id: int
    branch_id: int | None
    entity_type: str
    entity_id: str
    action: str


class AuditListResponse(BaseModel):
    items: list[AuditEntryResponse]
    total: int
