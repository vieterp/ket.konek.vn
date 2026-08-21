"""Bản cài `TreasurerCashBook` — cửa ghi/gỡ dòng sổ quỹ (SRS 17 §2, lát 6C).

Bảng `treasurer_cash_book` thuộc module này; hai người gọi đi qua kernel
Protocol thay vì đụng bảng trực tiếp (luật phụ thuộc #1):

* `warehousing.treasurer.queue_service` — thủ quỹ Ghi sổ quỹ (phân hệ bật);
* `cash_book.treasurer_source` — phiếu vào thẳng sổ quỹ khi phân hệ tắt
  (FR-WHK-021) và gỡ dòng khi bỏ ghi sổ kế toán.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.protocols import TreasurerBookEntry
from ket.modules.warehousing.treasurer.models import TreasurerCashBookEntry


class TreasurerCashBookImpl:
    """Ghi/gỡ dòng sổ quỹ — mỗi phiếu đúng một dòng (UNIQUE `voucher_id`)."""

    def record(self, session: Session, entry: TreasurerBookEntry) -> None:
        session.add(
            TreasurerCashBookEntry(
                branch_id=entry.branch_id,
                cash_account_id=entry.cash_account_id,
                book_date=entry.book_date,
                voucher_id=entry.voucher_id,
                receipt_amount=entry.receipt_amount,
                payment_amount=entry.payment_amount,
                posted_by=entry.posted_by,
            )
        )
        # Flush ngay để UNIQUE `voucher_id` nổ trong lời gọi này (một phiếu ghi
        # sổ quỹ hai lần là lỗi thao tác phải chặn tại chỗ), không nổ muộn ở
        # commit nơi thông điệp mất ngữ cảnh.
        session.flush()

    def erase(self, session: Session, *, voucher_id: UUID) -> None:
        # Xóa qua ORM chứ không `DELETE` set-based: listener nhật ký (`Audited`)
        # móc vào sự kiện ORM — gỡ một dòng sổ quỹ phải để lại vết ai gỡ, lúc nào.
        row = session.execute(
            select(TreasurerCashBookEntry).where(TreasurerCashBookEntry.voucher_id == voucher_id)
        ).scalar_one_or_none()
        if row is not None:
            session.delete(row)
            session.flush()
