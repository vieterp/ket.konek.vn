"""Bảng `attachments` — metadata của tệp đính kèm (FR-NFR-053).

Bản ghi ở đây trỏ tới một tệp trên đĩa bằng `content_hash`, và **nhiều** bản ghi
trỏ chung một tệp là chuyện bình thường: cùng một hợp đồng scan đính vào phiếu
chi lẫn hóa đơn thì chỉ có một tệp. Đó cũng là lý do gỡ đính kèm **không** xóa
tệp — xem `detached_at`.

`entity_type`/`entity_id` dùng đúng quy ước của `audit_log` (tên bảng + khóa
chính dạng chuỗi), không phải khóa ngoại: bảng chứng từ chưa tồn tại ở phase
này, và từ phase 6 sẽ có hàng chục bảng chủ khác nhau — một khóa ngoại cho mỗi
bảng nghĩa là `ALTER TABLE` mỗi lần thêm loại chứng từ.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.persistence.base import DatasetBase

ATTACHMENT_TABLE_NAME = "attachments"

SHA256_HEX_LENGTH = 64


class Attachment(DatasetBase, Audited):
    """Một tệp đã đính vào một bản ghi nghiệp vụ.

    `Audited`: ai đính tệp gì vào chứng từ nào, và ai gỡ nó ra, là câu hỏi kiểm
    toán viên hỏi thật — đính kèm là nơi hợp đồng, ủy nhiệm chi và biên bản
    sống, nên một tệp lặng lẽ biến mất khỏi chứng từ phải để lại vết.
    """

    __tablename__ = ATTACHMENT_TABLE_NAME
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint(
            f"content_hash ~ '^[0-9a-f]{{{SHA256_HEX_LENGTH}}}$'",
            name="content_hash_is_sha256_hex",
        ),
        Index("ix_attachments_entity", "entity_type", "entity_id"),
        # Cùng một tệp không đính hai lần vào cùng một bản ghi **trong cùng một
        # chi nhánh**. Hai đặc điểm của index này, mỗi cái đóng một lỗ:
        #
        # * **Bộ phận** theo `detached_at IS NULL`: gỡ ra rồi đính lại là thao
        #   tác sửa sai hợp lệ (đính nhầm chỗ), và một ràng buộc toàn phần sẽ
        #   chặn đúng lần sửa đó trong khi lịch sử vẫn phải giữ dòng đã gỡ.
        # * **Mang `branch_id`**: ràng buộc duy nhất chạy **ngoài** RLS, nên một
        #   khóa không có chi nhánh biến thành máy soi — chi nhánh B đính tệp và
        #   nhận `409` là biết chi nhánh A đã đính đúng tệp đó vào đúng bản ghi
        #   đó, dù B không được phép thấy dòng nào của A. Thêm chi nhánh vào
        #   khóa vừa gỡ máy soi vừa gỡ chỗ chặn: hai chi nhánh là hai ngăn.
        Index(
            "uq_attachments_live",
            "branch_id",
            "entity_type",
            "entity_id",
            "content_hash",
            unique=True,
            postgresql_where=text("detached_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    entity_type: Mapped[str] = mapped_column(String(150), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)

    content_hash: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    """SHA-256 hex của nội dung — vừa là khóa của tệp trên đĩa, vừa là bằng chứng
    tệp tải về đúng bằng tệp đã tải lên."""

    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(150), nullable=False)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    """Tên người dùng thấy. **Không** dùng để dựng đường dẫn — đường dẫn trên đĩa
    sinh từ `content_hash` (xem `storage.py`), nên một tên tệp chứa `..` hay dấu
    gạch chéo không chạm tới hệ tệp được."""

    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """Chi nhánh của bản ghi chủ — neo cô lập RLS.

    `NOT NULL` khác `audit_log`/`jobs` (nơi NULL nghĩa là "sự kiện mức hệ
    thống"): một tệp đính kèm luôn thuộc về một chứng từ hoặc một danh mục của
    một chi nhánh cụ thể, và cho phép NULL ở đây là mở một ngăn mà mọi chi nhánh
    cùng nhìn thấy — đúng thứ RT-04 cấm."""

    uploaded_by: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Lúc tệp bị gỡ khỏi bản ghi chủ. `NULL` = đang đính.

    Gỡ chứ không xóa dòng: nhật ký kiểm toán ghi "đã gỡ tệp X khỏi chứng từ Y",
    và câu đó chỉ tra ngược được nếu dòng metadata còn. Tệp trên đĩa cũng ở lại —
    bản ghi khác có thể đang trỏ vào chính nó."""

    detached_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
