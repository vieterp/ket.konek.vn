"""Sổ quỹ của thủ quỹ (SRS 17 §2 — hai sổ song song, FR-WHK-001..005).

Vì sao là một bảng thật chứ không một view trên `cash_vouchers`: SRS 17 nói
thẳng — sổ kế toán và sổ quỹ là **hai sổ do hai người giữ**, và giá trị kiểm
soát nằm ở chỗ chúng có thể **lệch nhau**. Một view thì không bao giờ lệch,
tức là không kiểm soát được gì. Báo cáo chênh lệch (FR-WHK-023, BR-WHK-03) và
check toàn vẹn phase 4 so hai bảng này với nhau.

Append theo lượt ghi sổ của thủ quỹ (`queue_service`, lát 6C); mỗi phiếu đúng
một dòng (`UNIQUE voucher_id`) — thủ quỹ ghi sổ một phiếu hai lần là lỗi thao
tác phải chặn ở DB, không phải một dòng trùng lặng lẽ làm sổ quỹ phồng lên.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.persistence.base import DatasetBase
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE


class TreasurerCashBookEntry(DatasetBase, Audited):
    """Một dòng sổ quỹ: phiếu nào, ngày nào, thu hay chi bao nhiêu."""

    __tablename__ = "treasurer_cash_book"
    __table_args__ = (
        CheckConstraint("receipt_amount >= 0 AND payment_amount >= 0", name="amounts_not_negative"),
        # Một dòng sổ quỹ là thu HOẶC chi — phiếu thu không có phần chi. Cho
        # phép cả hai bằng 0 thì không: dòng như vậy không kể chuyện gì.
        CheckConstraint("(receipt_amount > 0) <> (payment_amount > 0)", name="exactly_one_side"),
        UniqueConstraint("voucher_id", name="uq_treasurer_cash_book_voucher"),
        Index("ix_treasurer_cash_book_account_date", "cash_account_id", "book_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    """Chi nhánh của phiếu — bật RLS như mọi bảng phát sinh mang `branch_id`."""

    cash_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    book_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Ngày ghi sổ quỹ — theo ngày hạch toán hoặc ngày thủ quỹ chọn, không nhỏ
    hơn ngày hạch toán (BR-WHK-05, service kiểm)."""

    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=False
    )
    """`RESTRICT`: phiếu đã lên sổ quỹ thì không xóa được lặng lẽ — muốn gỡ
    phải đi đường bỏ ghi sổ, nơi có kiểm quyền và nhật ký."""

    receipt_amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    payment_amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )

    posted_by: Mapped[int] = mapped_column(Integer, nullable=False)
    """`user_id` trần như mọi tham chiếu `public.users` — thủ quỹ (hoặc hệ
    thống khi phân hệ tắt, FR-WHK-021)."""

    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
