"""Nối kho tệp với bảng metadata (FR-NFR-053).

Thứ tự **ghi đĩa trước, ghi bảng sau** là quyết định trung tâm của tệp này, và
nó không đối xứng:

* Ghi đĩa trước rồi transaction hỏng → còn lại một tệp không ai trỏ tới. Vô
  hình với người dùng, tốn ít đĩa, dọn được bằng một lượt quét đối chiếu
  (phase 11).
* Ghi bảng trước rồi ghi đĩa hỏng → có dòng metadata trỏ vào hư không. Người
  dùng thấy tệp trong danh sách, bấm tải về, nhận lỗi — và không có cách nào
  biết đó là tệp nào để tải lên lại.

Hướng hỏng thứ nhất rẻ hơn hẳn, nên đó là hướng được chọn.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.attachments.models import Attachment
from ket.kernel.errors import AttachmentAlreadyAttachedError, AttachmentNotFoundError

LIVE_ATTACHMENT_CONSTRAINT = "uq_attachments_live"


def attach(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    content_hash: str,
    byte_size: int,
    media_type: str,
    file_name: str,
    branch_id: int,
    user_id: int,
) -> Attachment:
    """Ghi metadata cho một tệp **đã nằm trên đĩa**.

    Kiểm trùng bằng câu truy vấn trước, dù index bộ phận `uq_attachments_live`
    đã chặn ở tầng DB: hai lớp trả lời hai câu khác nhau. Index giữ bất biến kể
    cả dưới truy cập song song; câu truy vấn ở đây cho người dùng một thông điệp
    nêu đúng việc đã xảy ra thay vì một lỗi ràng buộc thô.

    Nhánh song song — hai request cùng đính một tệp, cả hai cùng thấy trống —
    được gom về **cùng một** lỗi bằng savepoint quanh lệnh ghi. Không có nó,
    cùng một tình huống trả hai mã lỗi khác nhau tùy nhịp gõ của người dùng, và
    mã của nhánh hiếm hơn (`data.duplicate`) lại là mã không nói được chuyện gì
    đã xảy ra.
    """
    existing = session.scalar(
        select(Attachment).where(
            Attachment.entity_type == entity_type,
            Attachment.entity_id == entity_id,
            Attachment.content_hash == content_hash,
            Attachment.detached_at.is_(None),
        )
    )
    if existing is not None:
        raise AttachmentAlreadyAttachedError(
            "Tệp này đã được đính kèm vào bản ghi", attachment=existing.id
        )

    attachment = Attachment(
        entity_type=entity_type,
        entity_id=entity_id,
        content_hash=content_hash,
        byte_size=byte_size,
        media_type=media_type,
        file_name=file_name,
        branch_id=branch_id,
        uploaded_by=user_id,
    )
    # Savepoint chứ không `try` trần: một `IntegrityError` làm hỏng cả
    # transaction ở PostgreSQL, nên bắt nó mà không có điểm quay lui thì mọi
    # lệnh sau đó — kể cả lệnh ghi khóa idempotency — đều đổ theo.
    try:
        with session.begin_nested():
            session.add(attachment)
            session.flush()
    except IntegrityError as conflict:
        if _violates(conflict, LIVE_ATTACHMENT_CONSTRAINT):
            raise AttachmentAlreadyAttachedError(
                "Tệp này đã được đính kèm vào bản ghi"
            ) from conflict
        raise
    return attachment


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Lỗi ràng buộc này có phải của đúng ràng buộc đang quan tâm không.

    Đọc `diag.constraint_name` chứ không dò chuỗi thông điệp: thông điệp của
    PostgreSQL đổi theo phiên bản và theo `lc_messages` của cụm — một phép dò
    chuỗi sẽ im lặng ngừng khớp ở đúng bản cài dùng tiếng khác.
    """
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None) == constraint


def list_for_entity(session: Session, *, entity_type: str, entity_id: str) -> Sequence[Attachment]:
    """Tệp **đang đính** vào một bản ghi, cũ nhất trước.

    RLS lọc theo chi nhánh trước khi mã này chạy, nên "mọi tệp" ở đây nghĩa là
    mọi tệp người gọi được thấy. Dòng đã gỡ không trả về: chúng là lịch sử, và
    nơi đọc lịch sử là nhật ký kiểm toán.
    """
    return session.scalars(
        select(Attachment)
        .where(
            Attachment.entity_type == entity_type,
            Attachment.entity_id == entity_id,
            Attachment.detached_at.is_(None),
        )
        .order_by(Attachment.id)
    ).all()


def get_attachment(session: Session, attachment_id: int) -> Attachment:
    """Một tệp đính kèm theo id, kể cả dòng đã gỡ.

    Kể cả dòng đã gỡ vì đường tải về của một dòng đã gỡ vẫn có ích: người vừa
    lỡ tay gỡ nhầm cần lấy lại tệp. Ai được gọi là câu hỏi của RBAC + RLS, đã
    trả lời trước khi tới đây.
    """
    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise AttachmentNotFoundError("Không tìm thấy tệp đính kèm", attachment=attachment_id)
    return attachment


def detach(session: Session, *, attachment_id: int, user_id: int) -> Attachment:
    """Gỡ tệp khỏi bản ghi chủ. Tệp trên đĩa **ở lại**.

    Gỡ hai lần không phải lỗi: trạng thái đích đã đạt, và biến một thao tác dọn
    dẹp thành lỗi chỉ làm khó người dùng bấm hai lần vì mạng chậm.
    """
    attachment = get_attachment(session, attachment_id)
    if attachment.detached_at is None:
        attachment.detached_at = datetime.now(UTC)
        attachment.detached_by = user_id
        session.flush()
    return attachment
