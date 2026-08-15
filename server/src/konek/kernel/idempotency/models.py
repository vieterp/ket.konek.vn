"""Khóa idempotency — chống ghi trùng khi mạng LAN chập chờn (FR-NFR-004, RT-12).

Tình huống thật: kế toán bấm "Ghi sổ", switch nháy, client không nhận được phản
hồi và gửi lại. Không có khóa thì có hai phiếu chi giống hệt nhau.

Bốn quyết định của RT-12 nằm ngay trong hình dạng bảng này:

1. **Cùng transaction.** Dòng khóa được `INSERT` trong **chính** transaction
   ghi nghiệp vụ. Bảng riêng commit trước sẽ để lại khóa cho một chứng từ chưa
   từng tồn tại — và lần thử lại sẽ bị từ chối mà không có gì để trả về.
2. **Phạm vi theo route**, không theo toàn hệ thống: khóa chính là
   `(route_key, idempotency_key)`. Client sinh khóa ngẫu nhiên trùng nhau giữa
   hai màn hình khác nhau là chuyện có thật.
3. **Hết hạn thì fail-closed.** `expires_at` chỉ để dọn dẹp; khi khóa đã hết
   hạn mà request cũ quay lại, tầng dịch vụ **từ chối** thay vì chạy lại mù —
   chạy lại một lệnh ghi sổ sau 25 giờ là ghi trùng, không phải khôi phục.
4. **Lưu tham chiếu, không lưu phản hồi thô.** Chỉ loại + khóa của bản ghi kết
   quả. Phản hồi thô phình bảng, lỗi thời khi chứng từ đổi, và có thể chứa dữ
   liệu mà người gọi lại lần hai không còn quyền xem.

Dịch vụ dùng bảng này ở bước 9 (slice sau). Bảng vào `0001` cùng đợt với các
bảng nền khác.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from konek.kernel.persistence.base import DatasetBase


class IdempotencyKey(DatasetBase):
    """Một lần thực hiện đã thành công của một thao tác đổi trạng thái."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_expires_at", "expires_at"),)

    route_key: Mapped[str] = mapped_column(String(150), primary_key=True)
    """Định danh ổn định của endpoint, ví dụ `POST /cash-book/receipts/{id}/actions/post`.
    Dùng route **khai báo** chứ không phải URL đã điền tham số — nếu không, mỗi
    chứng từ lại thành một phạm vi riêng và khóa mất tác dụng."""

    idempotency_key: Mapped[str] = mapped_column(String(120), primary_key=True)

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)

    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    """Băm của thân request. Cùng khóa nhưng khác nội dung = lỗi phía client
    (dùng lại khóa), phải trả `409` chứ không phải im lặng trả kết quả cũ."""

    result_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result_id: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
