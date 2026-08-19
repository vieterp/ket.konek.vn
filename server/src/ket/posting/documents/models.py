"""Header chứng từ dùng chung `vouchers` (ADR-005, SRS 00 §3.3).

Một header cho **mọi** loại chứng từ; bảng chi tiết thuộc từng module
(`gl_journal_lines`, `cash_voucher_lines`, …) và posting engine không bao giờ
đọc chúng — module tự dịch chi tiết thành `PostingRequest`.

`id` là UUIDv7 sinh ở tầng ứng dụng (ADR-012): chứng từ được tham chiếu từ
`gl_postings`, từ đính kèm, từ khóa idempotency — một khóa thời-gian-đầu giữ
index chèn cuối cây như khóa tăng dần, mà vẫn ổn định khi tổng hợp nhiều bản
cài (RT-19).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import IntEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.identifiers import uuid7
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

DOCUMENT_TYPE_MAX_LENGTH = 20
VOUCHER_NO_MAX_LENGTH = 50
DESCRIPTION_MAX_LENGTH = 1000


class VoucherStatus(IntEnum):
    """Trạng thái lưu của chứng từ (SRS 00 §3.3).

    Giá trị số theo phase-04 (`0 Nhập … 4 Đã hủy`). **Không có thành viên cho
    0**: "Nhập" là trạng thái của form đang mở trên màn hình — chứng từ chưa
    từng được Cất thì chưa có dòng nào trong bảng, nên một hằng cho nó chỉ tạo
    ra nhánh mã không bao giờ chạy. Dãy số giữ nguyên để 0 vẫn dành sẵn cho
    nghĩa đó nếu về sau có lưu nháp.
    """

    DA_CAT = 1
    """Đã cất — có số chứng từ, chưa lên sổ. Sửa/xóa tự do khi kỳ còn mở."""

    DA_GHI_SO = 2
    """Đã ghi sổ — có dòng trong `gl_postings`, xuất hiện trên báo cáo (N1)."""

    DA_KHOA_SO = 3
    """Thuộc kỳ đã khóa — bất động cho tới khi kỳ được mở lại.

    **Không bao giờ được ghi vào cột `status`** (quyết định 4D): nguồn sự thật
    là `accounting_periods.locked_at`, và trạng thái này là dữ liệu **suy ra**
    lúc đọc (chứng từ Đã ghi sổ + kỳ của nó đã khóa). Ghi nó xuống hàng nghĩa
    là mỗi lần khóa/mở kỳ phải UPDATE hàng loạt voucher — O(n) giữ `FOR UPDATE`
    trên dòng kỳ, một trận diff vô nghĩa vào `audit_log`, và hai nguồn sự thật
    thì một trong hai sẽ nói dối. Giá trị 3 giữ chỗ trong dãy để client nào
    muốn hiển thị nó có một mã ổn định."""

    DA_HUY = 4
    """Đã hủy — giữ số chứng từ (số không tái sử dụng), dùng từ phase 7."""


class Voucher(DatasetBase, Audited, RowVersioned):
    """Header của một chứng từ, mọi loại, mọi phân hệ.

    `Audited` + `RowVersioned`: đây là bảng con người sửa qua form, và chuyển
    trạng thái Cất/Ghi sổ/Bỏ ghi sổ chính là vết mà FR-NFR-012/013 đòi —
    listener ghi diff `status`/`posted_at`/`posted_by` cùng transaction.
    """

    __tablename__ = "vouchers"
    __table_args__ = (
        CheckConstraint(
            f"status BETWEEN {VoucherStatus.DA_CAT} AND {VoucherStatus.DA_HUY}",
            name="status_known",
        ),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        # Cùng lập luận `lock_stamp_complete` của kỳ kế toán: "đã ghi sổ nhưng
        # không biết ai ghi" là nửa dữ liệu chỉ lộ ra lúc cần giải trình.
        CheckConstraint("(posted_at IS NULL) = (posted_by IS NULL)", name="posted_stamp_complete"),
        UniqueConstraint(
            "document_type", "branch_id", "voucher_no", name="uq_vouchers_type_branch_no"
        ),
        Index("ix_vouchers_period", "period_id", "document_type", "status"),
        Index("ix_vouchers_posting_date", "posting_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)

    document_type: Mapped[str] = mapped_column(String(DOCUMENT_TYPE_MAX_LENGTH), nullable=False)
    """Mã loại chứng từ (`GLE`, `PT`, `PC`, …) — trùng `code` trong registry
    phân quyền của module sở hữu."""

    voucher_no: Mapped[str] = mapped_column(String(VOUCHER_NO_MAX_LENGTH), nullable=False)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )

    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Ngày ghi trên chứng từ giấy."""

    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Ngày hạch toán — quyết định kỳ. Hai ngày tách nhau vì hóa đơn tháng
    trước vẫn hạch toán vào tháng này là chuyện hằng ngày của kế toán VN."""

    period_id: Mapped[int] = mapped_column(
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False
    )

    currency_code: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        ForeignKey("currencies.code", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False, default=Decimal(1)
    )

    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    cashflow_activity: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """Hoạt động dòng tiền cho báo cáo LCTT (FR-GLE-044) — phase 10a đọc."""

    source_document_id: Mapped[UUID | None] = mapped_column(nullable=True)
    """Chứng từ nguồn sinh ra chứng từ này (phiếu chi trả cho hóa đơn mua) —
    không FK vì có thể trỏ vào bảng của module khác."""

    reversal_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=True
    )
    """Chứng từ mà bút toán này đảo — chỉ dùng khi kỳ đã khóa mà vẫn phải điều
    chỉnh (lệch có chủ đích so với research, xem phase-04 §Bảng phát sinh chung)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """`user_id` trần như mọi tham chiếu `public.users` (xem `persistence/base.py`).
    Cặp cột sống chết cùng nhau — ràng buộc `posted_stamp_complete` ở trên."""
