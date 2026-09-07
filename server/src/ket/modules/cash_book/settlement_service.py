"""Đối trừ công nợ của phiếu thu/chi (`docs/srs/03` §4, FR-QUY-003, FR-SYS-066).

Từ lát 6C phần không-phụ-thuộc-module (định giá, kiểm BR-QUY-02/03, cộng/gỡ
số đã trả, gộp nguồn hóa đơn còn nợ) sống ở `ket.posting.settlements` — chứng
từ tiền gửi (module `bank`) dùng cùng cơ chế mà không import được module này.
Ở đây chỉ còn phần buộc vào bảng `cash_settlements` và hình dạng payload phiếu.

Ba việc, ba thời điểm — xem docstring `ket.posting.settlements`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.modules.cash_book.models import (
    CashSettlement,
    CashVoucherKind,
    CashVoucherLine,
)
from ket.modules.cash_book.schemas import CashVoucherIn
from ket.posting.debt_lines import (
    money_voucher_settles_advance,
    record_pair_voucher_debt,
    remove_voucher_debt,
    settlement_scope_of,
    sides_of_payload_lines,
)
from ket.posting.documents.models import Voucher
from ket.posting.settlements import (
    SETTLEMENT_BRANCH_MISMATCH_CODE,
    SETTLEMENT_CURRENCY_MISMATCH_CODE,
    SETTLEMENT_NO_SOURCE_CODE,
    SETTLEMENT_OVER_REMAINING_CODE,
    SETTLEMENT_PARTNER_MISMATCH_CODE,
    SETTLEMENT_PARTNER_REQUIRED_CODE,
    SETTLEMENT_TARGET_MISSING_CODE,
    SETTLEMENT_TOTAL_MISMATCH_CODE,
    PricedSettlement,
    apply_settlement_rows,
    open_invoices,
    revert_settlement_rows,
)
from ket.posting.settlements import (
    price_settlements as price_settlement_inputs,
)

__all__ = [
    "SETTLEMENT_BRANCH_MISMATCH_CODE",
    "SETTLEMENT_CURRENCY_MISMATCH_CODE",
    "SETTLEMENT_NO_SOURCE_CODE",
    "SETTLEMENT_OVER_REMAINING_CODE",
    "SETTLEMENT_PARTNER_MISMATCH_CODE",
    "SETTLEMENT_PARTNER_REQUIRED_CODE",
    "SETTLEMENT_TARGET_MISSING_CODE",
    "SETTLEMENT_TOTAL_MISMATCH_CODE",
    "PricedSettlement",
    "apply_settlements",
    "clear_subledger_after_unpost",
    "open_invoices",
    "price_settlements",
    "revert_settlements",
    "sync_subledger_after_post",
]


def price_settlements(
    session: Session, payload: CashVoucherIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một phiếu (BR-QUY-02/03)."""
    # Phạm vi đo theo DÒNG CÔNG NỢ, không theo tổng tiền chứng từ (lát 7C-5,
    # điều kiện #9): khối đối trừ nhận đối tác ở header còn sổ cái ghi theo đối
    # tác từng dòng, nên chỉ tổng dòng công nợ mới là thứ hai vế cùng nhích.
    settles_advance = money_voucher_settles_advance(
        money_in=payload.kind == CashVoucherKind.RECEIPT,
        partner_kind=payload.partner_kind,
    )
    scope = settlement_scope_of(
        session,
        has_settlements=bool(payload.settlements),
        settles_advance=settles_advance,
        sides=sides_of_payload_lines(
            payload.lines,
            currency_code=payload.currency_code,
            exchange_rate=payload.exchange_rate,
        ),
        partner_kind=payload.partner_kind,
        partner_id=payload.partner_id,
    )
    return price_settlement_inputs(
        session,
        settlements=payload.settlements,
        lines_total_fc=scope.total_fc,
        partner_id=payload.partner_id,
        partner_kind=payload.partner_kind,
        branch_id=payload.branch_id,
        currency_code=payload.currency_code,
        exchange_rate=payload.exchange_rate,
        account_id=scope.account_id,
        scale=scale,
        # Phiếu chi cho khách và phiếu thu của người bán tất toán khoản ỨNG
        # TRƯỚC, không phải khoản nợ (lát 7C-4): hai thứ ấy nằm trên cùng TK và
        # cùng đối tác nên không phép kiểm nào khác tách được chúng. Cùng giá
        # trị ấy khóa CHIỀU của dòng công nợ ở `settlement_scope_of` (7C-5).
        settles_advance=settles_advance,
    )


def sync_subledger_after_post(session: Session, voucher_id: UUID, *, scale: int) -> None:
    """Ghi khoản công nợ phiếu sinh ra vào sổ phụ — chạy SAU `PostingService.post`.

    Phiếu thu Có 131 mang đối tác mà không chọn đối trừ là khoản khách **ứng
    trước**: nó nhích số dư 131 trên sổ cái, nên phải nhích cả sổ phụ, nếu
    không `arap_matches_control` đỏ trên dữ liệu ĐÚNG (điều kiện #2, mở từ 7A).
    """
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - engine vừa ghi sổ chính chứng từ này
        raise RuntimeError(f"Không tìm thấy phiếu thu/chi {voucher_id} để ghi sổ phụ")
    # Tiền tệ và tỷ giá đọc từ HEADER: dòng phiếu không mang tiền tệ, và thân
    # phiếu cũng không — cả hai rơi từ `vouchers` như mọi loại chứng từ khác.
    record_pair_voucher_debt(
        session,
        voucher=voucher,
        lines=_lines_of(session, voucher_id),
        currency_code=voucher.currency_code,
        exchange_rate=voucher.exchange_rate,
        scale=scale,
        has_settlements=bool(_settlements_of(session, voucher_id)),
    )


def clear_subledger_after_unpost(session: Session, voucher_id: UUID) -> None:
    """Gỡ dòng sổ phụ của phiếu — chạy SAU `PostingService.unpost`."""
    remove_voucher_debt(session, voucher_id=voucher_id)


def _lines_of(session: Session, voucher_id: UUID) -> Sequence[CashVoucherLine]:
    return (
        session.execute(
            select(CashVoucherLine)
            .where(CashVoucherLine.voucher_id == voucher_id)
            .order_by(CashVoucherLine.line_no)
        )
        .scalars()
        .all()
    )


def _settlements_of(session: Session, voucher_id: UUID) -> Sequence[CashSettlement]:
    return (
        session.execute(select(CashSettlement).where(CashSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã trả vào từng đích — chạy SAU `PostingService.post`, cùng transaction."""
    apply_settlement_rows(session, _settlements_of(session, voucher_id))


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ số đã trả — chạy SAU `PostingService.unpost`, cùng transaction."""
    revert_settlement_rows(session, _settlements_of(session, voucher_id))
