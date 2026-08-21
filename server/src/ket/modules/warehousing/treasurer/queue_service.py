"""Hàng đợi thủ quỹ + ghi sổ hàng loạt (SRS 17 §3.1, FR-WHK-001..003, lát 6C).

Phân vai với `cash_book` (qua kernel Protocol, luật phụ thuộc #1):

* nguồn phiếu (`TreasurerVoucherSource`, `cash_book` cài) kiểm trạng thái +
  BR-WHK-05 + lật `treasurer_status` — trạng thái sống trên thân phiếu;
* module này ghi dòng vào sổ quỹ của mình (`TreasurerCashBookImpl`) và trả kết
  quả từng phiếu cho UI.

Ghi sổ hàng loạt là **một transaction**: 50 phiếu cùng sống cùng chết
(FR-WHK-002 — "chọn nhiều chứng từ → Ghi sổ" là MỘT thao tác của thủ quỹ; nửa
danh sách vào sổ nửa báo lỗi là trạng thái không ai đối chiếu nổi). Phiếu đầu
tiên vi phạm làm cả lượt dừng với thông điệp nêu đích danh phiếu đó.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import TreasurerVoucherStateError
from ket.kernel.protocols import (
    PROVIDERS,
    TreasurerBookEntry,
    TreasurerPendingVoucher,
)
from ket.modules.warehousing.treasurer.book import TreasurerCashBookImpl
from ket.modules.warehousing.treasurer.models import TreasurerCashBookEntry


def pending_queue(session: Session) -> Sequence[TreasurerPendingVoucher]:
    """Phiếu đã ghi sổ kế toán, chờ thủ quỹ (FR-WHK-001) — RLS đã lọc chi nhánh.

    Không đòi phân hệ bật: sau khi tắt giữa chừng vẫn còn phiếu treo trạng thái
    chờ, và màn hình phải nhìn thấy chúng để xử lý nốt thay vì mất dấu.
    """
    source = PROVIDERS.treasurer_voucher_source()
    if source is None:
        return ()
    return source.pending(session)


def book_vouchers(
    session: Session,
    *,
    voucher_ids: Sequence[UUID],
    book_date: date | None,
    user_id: int,
) -> list[TreasurerBookEntry]:
    """Ghi sổ quỹ hàng loạt (FR-WHK-002/003) — một transaction cho cả lô.

    `book_date=None` = theo ngày hạch toán trên từng chứng từ; một ngày cụ thể
    áp cho cả lô và phải ≥ ngày hạch toán của TỪNG phiếu (BR-WHK-05, nguồn kiểm).

    **Không đòi phân hệ bật** (review 6C, M-1): tắt `treasurer.enabled` giữa
    chừng để lại phiếu treo trạng thái chờ; hàng đợi cho thấy chúng thì phải
    ghi nốt được, nếu không sổ quỹ thiếu vĩnh viễn các phiếu đó. Phiếu ghi sổ
    kế toán SAU khi tắt tự vào sổ quỹ (FR-WHK-021) nên hàng đợi tự cạn — cổng
    "phân hệ tắt" tự nhiên là hàng-đợi-rỗng, không cần một cổng cấu hình chặn
    thao tác dọn dẹp hợp lệ.
    """
    if not voucher_ids:
        raise TreasurerVoucherStateError("Chưa chọn phiếu nào để ghi sổ quỹ")
    if len(set(voucher_ids)) != len(voucher_ids):
        raise TreasurerVoucherStateError(
            "Danh sách phiếu ghi sổ quỹ có phiếu trùng lặp", count=len(voucher_ids)
        )
    source = PROVIDERS.treasurer_voucher_source()
    if source is None:  # pragma: no cover - model_registry luôn nạp cash_book
        raise RuntimeError("Không có TreasurerVoucherSource trong tiến trình")
    book = TreasurerCashBookImpl()
    entries_by_id: dict[UUID, TreasurerBookEntry] = {}
    # Khóa theo thứ tự ỔN ĐỊNH (sort id) chứ không theo thứ tự người gọi đưa:
    # hai lô đồng thời chọn chồng nhau theo hai thứ tự ngược nhau là deadlock
    # 40P01 → 500 (review 6C, LOW-4).
    for voucher_id in sorted(voucher_ids):
        entry = source.book(session, voucher_id=voucher_id, book_date=book_date, user_id=user_id)
        book.record(session, entry)
        entries_by_id[voucher_id] = entry
    return [entries_by_id[voucher_id] for voucher_id in voucher_ids]


def cash_book_rows(
    session: Session,
    *,
    cash_account_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> Sequence[TreasurerCashBookEntry]:
    """Sổ quỹ tiền mặt của thủ quỹ (FR-WHK-005) — dòng thô theo ngày ghi sổ.

    Số dư lũy kế là việc của tầng đọc (client cộng dồn trên trang, báo cáo
    metadata ở lát 6E) — service trả dữ liệu, không trả cách trình bày.
    """
    query = select(TreasurerCashBookEntry).order_by(
        TreasurerCashBookEntry.book_date, TreasurerCashBookEntry.id
    )
    if cash_account_id is not None:
        query = query.where(TreasurerCashBookEntry.cash_account_id == cash_account_id)
    if from_date is not None:
        query = query.where(TreasurerCashBookEntry.book_date >= from_date)
    if to_date is not None:
        query = query.where(TreasurerCashBookEntry.book_date <= to_date)
    return session.execute(query).scalars().all()
