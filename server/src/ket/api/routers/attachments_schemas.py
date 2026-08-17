"""Khuôn phản hồi của nhóm `/api/v1/attachments` (ADR-015: Pydantic ở mọi ranh giới).

Không có khuôn *request*: lượt tải lên đi bằng `multipart/form-data` nên tham số
nằm ở form field và tệp, không phải ở một thân JSON.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from ket.kernel.attachments.models import Attachment


class AttachmentResponse(BaseModel):
    """Metadata của một tệp đính kèm — **không** kèm nội dung.

    Nội dung tải riêng qua `/{id}/content`: danh sách đính kèm của một chứng từ
    hiện ra cùng màn hình chứng từ, và nhúng vài chục MB base64 vào đó là cách
    chắc chắn nhất để màn hình ấy chậm.
    """

    id: int
    entity_type: str
    entity_id: str
    file_name: str
    media_type: str
    byte_size: int
    content_hash: str
    branch_id: int
    uploaded_by: int
    uploaded_at: datetime
    detached_at: datetime | None

    @classmethod
    def of(cls, attachment: Attachment) -> AttachmentResponse:
        return cls(
            id=attachment.id,
            entity_type=attachment.entity_type,
            entity_id=attachment.entity_id,
            file_name=attachment.file_name,
            media_type=attachment.media_type,
            byte_size=attachment.byte_size,
            content_hash=attachment.content_hash,
            branch_id=attachment.branch_id,
            uploaded_by=attachment.uploaded_by,
            uploaded_at=attachment.uploaded_at,
            detached_at=attachment.detached_at,
        )


class AttachmentListResponse(BaseModel):
    items: list[AttachmentResponse]
