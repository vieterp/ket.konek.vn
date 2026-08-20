"""Bảng `print_log` — sổ theo dõi lần in chứng từ (FR-RPT-011).

**Chỉ-thêm** (`grants.APPEND_ONLY_TABLES`): mỗi lần in là một sự kiện đã xảy
ra — sửa/xóa dòng log là xóa bằng chứng "bản in số 2 tồn tại", đúng thứ kiểm
soát in sinh ra để giữ. RLS theo chi nhánh trên `branch_id` (denormalize từ
voucher lúc ghi): người chi nhánh A không thấy lịch sử in chứng từ chi nhánh B.

Không mixin `Audited` — cùng lập luận `allocated_numbers`: dòng log chính LÀ
dấu vết, ghi vết cho một bảng chỉ-thêm là ghi vết hai lần một sự việc.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.config.reports.models import REPORT_CODE_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase


class PrintLog(DatasetBase):
    __tablename__ = "print_log"
    __table_args__ = (
        CheckConstraint("copy_no >= 1", name="copy_no_positive"),
        # Hàng rào cuối cho phép đếm lần in: dịch vụ đã khóa dòng voucher
        # (`FOR UPDATE`) trước khi đếm, nên ràng buộc này lẽ ra không bao giờ
        # chạm tới — nó tồn tại cho ngày một đường in mới quên khóa.
        Index("uq_print_log_voucher_copy", "voucher_id", "copy_no", unique=True),
        Index("ix_print_log_voucher", "voucher_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    voucher_id: Mapped[UUID] = mapped_column(
        # CASCADE chứ không RESTRICT — QUYẾT ĐỊNH TƯỜNG MINH (review 5D, M3),
        # phủ cả hai đường xóa: (1) nháp in thử rồi xóa; (2) post → in → unpost
        # → delete. Ở đường (2), lịch sử SỐ LẦN IN đi theo chứng từ bị xóa —
        # chấp nhận vì: vết kiểm toán thật nằm ở `audit_log` bất biến (mọi
        # chuyển trạng thái + lệnh xóa đều có dòng, FR-NFR-012/013); `print_log`
        # là bộ đếm phục vụ CẢNH BÁO in lại của chứng từ CÒN SỐNG — chứng từ đã
        # xóa không còn lần in kế tiếp nào để cảnh báo, và số của nó không bao
        # giờ cấp lại. RESTRICT sẽ chặn chính việc xóa nháp hợp lệ; xóa log
        # trước rồi xóa voucher thì vai trò runtime không làm được (bảng
        # chỉ-thêm) — thành ra mọi nháp từng in thử là rác vĩnh viễn.
        ForeignKey("vouchers.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    template_code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), nullable=False)
    copy_no: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    printed_by: Mapped[int] = mapped_column(Integer, nullable=False)
    """`user_id` trần như mọi tham chiếu `public.users` (xem `persistence/base.py`)."""
