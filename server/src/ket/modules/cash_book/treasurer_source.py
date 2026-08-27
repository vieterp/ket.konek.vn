"""Cầu nối phiếu thu/chi ↔ sổ quỹ thủ quỹ (SRS 17 §2, lát 6C).

Hai chiều, hai vai:

* `CashTreasurerVoucherSource` cài `TreasurerVoucherSource` (kernel Protocol)
  cho hàng đợi của `warehousing`: liệt kê phiếu chờ (FR-WHK-001) và lật trạng
  thái khi thủ quỹ Ghi sổ quỹ — trạng thái thủ quỹ sống trên thân phiếu
  (`cash_vouchers.treasurer_status`) nên chỉ module này được viết nó.
* `sync_after_post` / `clear_after_unpost` — móc vào vòng đời ghi sổ của PT/PC:
  phân hệ **tắt** (FR-WHK-021) thì phiếu vào thẳng sổ quỹ ngay lúc ghi sổ kế
  toán (ghi qua Protocol `TreasurerCashBook`, bảng thuộc `warehousing`); bỏ ghi
  sổ thì gỡ dòng sổ quỹ và trả phiếu về trạng thái chờ.

Quyết định bỏ-ghi-sổ-thì-gỡ-sổ-quỹ (kể cả phiếu thủ quỹ ĐÃ ghi): SRS 17 không
có luồng "thủ quỹ gỡ ghi sổ", nên chặn bỏ ghi sổ kế toán chờ thủ quỹ gỡ trước
là một ngõ cụt thao tác. Gỡ theo — có nhật ký (bảng sổ quỹ `Audited`) — rồi
phiếu quay lại hàng đợi sau khi ghi sổ lại; sổ quỹ và sổ kế toán không bao giờ
giữ hai sự thật lệch nhau về một phiếu đã rút khỏi sổ cái.

Số tiền sổ quỹ = **VND quy đổi phần chạm TK quỹ** của phiếu: cộng các dòng có
bên Nợ = TK quỹ (tiền vào) và bên Có = TK quỹ (tiền ra) — cùng nguồn số mà sổ
chi tiết TK 111 đọc từ `gl_postings`, để BR-WHK-03 so hai sổ cùng một đơn vị.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import MONEY_SCALE_KEY, TREASURER_ENABLED_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    TreasurerBookDateInvalidError,
    TreasurerVoucherStateError,
)
from ket.kernel.money import convert_currency
from ket.kernel.protocols import (
    PROVIDERS,
    TreasurerBookEntry,
    TreasurerPendingVoucher,
)
from ket.modules.cash_book.models import (
    CashVoucher,
    CashVoucherLine,
    TreasurerStatus,
)
from ket.posting.contracts import Voucher
from ket.posting.documents.models import VoucherStatus

_ZERO = Decimal(0)


def treasurer_enabled(session: Session, *, user_id: int) -> bool:
    return value_of(session, key=TREASURER_ENABLED_KEY, user_id=user_id) is True


def _cash_side_totals(
    session: Session, voucher: Voucher, body: CashVoucher, *, user_id: int
) -> tuple[Decimal, Decimal]:
    """(tiền vào, tiền ra) VND quy đổi của phần chạm TK quỹ."""
    scale = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
        raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
    receipt = _ZERO
    payment = _ZERO
    lines = (
        session.execute(select(CashVoucherLine).where(CashVoucherLine.voucher_id == voucher.id))
        .scalars()
        .all()
    )
    for line in lines:
        amount = convert_currency(line.amount_fc, voucher.exchange_rate, scale)
        if line.debit_account_id == body.cash_account_id:
            receipt += amount
        if line.credit_account_id == body.cash_account_id:
            payment += amount
    return receipt, payment


def _book_entry(
    session: Session,
    voucher: Voucher,
    body: CashVoucher,
    *,
    book_date: date,
    user_id: int,
) -> TreasurerBookEntry | None:
    """Dòng sổ quỹ của một phiếu — `None` khi phiếu không chạm TK quỹ đồng nào
    (không có tiền thật di chuyển thì không có gì để thủ quỹ ghi)."""
    receipt, payment = _cash_side_totals(session, voucher, body, user_id=user_id)
    # Sổ quỹ ghi dòng thu HOẶC chi (CHECK `exactly_one_side`) — phiếu vừa vào
    # vừa ra quỹ (hiếm: định khoản tự do hai chiều) ghi theo số RÒNG.
    net = receipt - payment
    if net == 0:
        return None
    return TreasurerBookEntry(
        voucher_id=voucher.id,
        branch_id=voucher.branch_id,
        cash_account_id=body.cash_account_id,
        book_date=book_date,
        receipt_amount=net if net > 0 else _ZERO,
        payment_amount=-net if net < 0 else _ZERO,
        posted_by=user_id,
    )


def sync_after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    """Sau khi phiếu ghi sổ kế toán: xếp hàng đợi thủ quỹ, hoặc — phân hệ tắt
    (FR-WHK-021) — vào thẳng sổ quỹ theo ngày hạch toán, cùng transaction.

    Phiếu **net-0** (không đồng nào chạm TK quỹ của thân phiếu — định khoản tự
    do) đặt "không áp dụng" ở CẢ hai chế độ: không có tiền thật để thủ quỹ
    thu/chi thì không có gì để ghi sổ quỹ, và một phiếu như vậy nằm trong hàng
    đợi là một phiếu không bao giờ ghi được — nó đầu độc cả lô ghi-sổ-hàng-loạt
    all-or-nothing (review 6C, H-2).
    """
    body = session.get(CashVoucher, voucher_id)
    if body is None:  # pragma: no cover - hook chỉ chạy cho PT/PC
        raise RuntimeError(f"Phiếu thu/chi {voucher_id} thiếu thân")
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - hook chạy ngay sau post cùng scope
        raise RuntimeError(f"Phiếu thu/chi {voucher_id} thiếu header")
    entry = _book_entry(session, voucher, body, book_date=voucher.posting_date, user_id=user_id)
    if entry is None:
        body.treasurer_status = TreasurerStatus.NOT_APPLICABLE
        return
    if treasurer_enabled(session, user_id=user_id):
        body.treasurer_status = TreasurerStatus.PENDING
        return
    body.treasurer_status = TreasurerStatus.NOT_APPLICABLE
    book = PROVIDERS.treasurer_cash_book()
    if book is None:
        # Tiến trình không nạp module sổ quỹ (test kernel thuần) — không có
        # sổ để ghi, trạng thái "không áp dụng" vẫn đúng.
        return
    book.record(session, entry)
    session.flush()


def clear_after_unpost(session: Session, voucher_id: UUID, user_id: int) -> None:
    """Sau khi bỏ ghi sổ kế toán: gỡ dòng sổ quỹ (nếu có) + trả phiếu về trạng
    thái chờ — xem docstring đầu tệp về quyết định gỡ-theo."""
    body = session.get(CashVoucher, voucher_id)
    if body is None:  # pragma: no cover - hook chỉ chạy cho PT/PC
        raise RuntimeError(f"Phiếu thu/chi {voucher_id} thiếu thân")
    book = PROVIDERS.treasurer_cash_book()
    if book is not None:
        book.erase(session, voucher_id=voucher_id)
    body.treasurer_status = TreasurerStatus.PENDING
    body.treasurer_book_date = None
    body.treasurer_posted_at = None
    body.treasurer_posted_by = None
    session.flush()


class CashTreasurerVoucherSource:
    """Bản cài `TreasurerVoucherSource` — nguồn phiếu thu/chi cho hàng đợi."""

    def pending(self, session: Session) -> Sequence[TreasurerPendingVoucher]:
        rows = session.execute(
            select(Voucher, CashVoucher)
            .join(CashVoucher, CashVoucher.id == Voucher.id)
            .where(
                Voucher.status == VoucherStatus.DA_GHI_SO,
                CashVoucher.treasurer_status == TreasurerStatus.PENDING,
            )
            .order_by(Voucher.posting_date, Voucher.voucher_no)
        ).all()
        pending: list[TreasurerPendingVoucher] = []
        for voucher, body in rows:
            receipt, payment = _cash_side_totals(session, voucher, body, user_id=voucher.created_by)
            net = receipt - payment
            pending.append(
                TreasurerPendingVoucher(
                    voucher_id=voucher.id,
                    voucher_no=voucher.voucher_no,
                    document_type=voucher.document_type,
                    branch_id=voucher.branch_id,
                    posting_date=voucher.posting_date,
                    cash_account_id=body.cash_account_id,
                    is_receipt=net >= 0,
                    amount=abs(net),
                    payer_receiver_name=body.payer_receiver_name,
                    description=voucher.description,
                )
            )
        return pending

    def book(
        self,
        session: Session,
        *,
        voucher_id: UUID,
        book_date: date | None,
        user_id: int,
        today: date,
    ) -> TreasurerBookEntry:
        # Đọc HEADER trước — `vouchers` có RLS chi nhánh còn `cash_vouchers`
        # (bảng con, 0015) thì không: khóa thân trước là cầm `FOR UPDATE` trên
        # hàng của chi nhánh ngoài phạm vi rồi mới nổ (review 6C, H-1). Header
        # ẩn dưới RLS và "không phải phiếu quỹ" trả CÙNG một thông điệp — không
        # để lộ chứng từ ngoài phạm vi có tồn tại hay không.
        voucher = session.get(Voucher, voucher_id)
        if voucher is None:
            raise TreasurerVoucherStateError(
                "Chứng từ này không phải phiếu thu/chi tiền mặt trong phạm vi của bạn",
                voucher_id=str(voucher_id),
            )
        body = session.execute(
            select(CashVoucher).where(CashVoucher.id == voucher_id).with_for_update()
        ).scalar_one_or_none()
        # `FOR UPDATE` trên thân phiếu: hai thủ quỹ cùng ghi một phiếu thì người
        # sau đọc trạng thái ĐÃ lật và bị từ chối có thông điệp — UNIQUE của
        # bảng sổ quỹ là lưới cuối, không phải thông điệp lỗi.
        if body is None:
            raise TreasurerVoucherStateError(
                "Chứng từ này không phải phiếu thu/chi tiền mặt trong phạm vi của bạn",
                voucher_id=str(voucher_id),
            )
        # Header được đọc TRƯỚC khi cầm khóa thân — một lượt bỏ-ghi-sổ đồng thời
        # có thể đã commit trong lúc ta chờ khóa (nó cũng UPDATE thân phiếu nên
        # ta chờ nó). Đọc lại header sau khóa để kiểm trạng thái trên dữ liệu
        # đã commit, không phải bản chụp cũ trong identity map.
        session.refresh(voucher)
        if voucher.status != VoucherStatus.DA_GHI_SO:
            raise TreasurerVoucherStateError(
                "Phiếu chưa ghi sổ kế toán — sổ quỹ chỉ nhận phiếu đã vào sổ (BR-WHK-01)",
                voucher_no=voucher.voucher_no,
            )
        if body.treasurer_status != TreasurerStatus.PENDING:
            raise TreasurerVoucherStateError(
                "Phiếu không ở trạng thái chờ thủ quỹ — đã ghi sổ quỹ hoặc phân hệ tắt "
                "lúc phiếu ghi sổ",
                voucher_no=voucher.voucher_no,
                treasurer_status=body.treasurer_status,
            )
        effective_date = book_date if book_date is not None else voucher.posting_date
        if effective_date < voucher.posting_date:
            raise TreasurerBookDateInvalidError(
                "Ngày ghi sổ quỹ không được nhỏ hơn ngày hạch toán trên chứng từ (BR-WHK-05)",
                voucher_no=voucher.voucher_no,
                posting_date=voucher.posting_date.isoformat(),
                book_date=effective_date.isoformat(),
            )
        # Trần trên: sổ quỹ ghi việc ĐÃ làm — kể cả chế độ "theo ngày chứng từ"
        # (phiếu hạch toán ngày tương lai thì chờ đến ngày đó mới ghi sổ quỹ
        # được). Kiểm ở ĐÂY chứ không ở `_book_entry`: đường tự-ghi khi phân hệ
        # tắt là bản phản chiếu của việc ghi sổ kế toán, không được chặn nó.
        if effective_date > today:
            raise TreasurerBookDateInvalidError(
                "Ngày ghi sổ quỹ không được ở tương lai — sổ quỹ ghi việc đã làm",
                voucher_no=voucher.voucher_no,
                book_date=effective_date.isoformat(),
                today=today.isoformat(),
            )
        entry = _book_entry(session, voucher, body, book_date=effective_date, user_id=user_id)
        if entry is None:
            raise TreasurerVoucherStateError(
                "Phiếu không có đồng nào chạm TK quỹ — không có gì để ghi sổ quỹ",
                voucher_no=voucher.voucher_no,
            )
        body.treasurer_status = TreasurerStatus.BOOKED
        body.treasurer_book_date = effective_date
        body.treasurer_posted_by = user_id
        body.treasurer_posted_at = datetime.now(UTC)
        session.flush()
        return entry
