"""Đối trừ công nợ của phiếu thu/chi (`docs/srs/03` §4, FR-QUY-003, FR-SYS-066).

Từ lát 6C phần không-phụ-thuộc-module (định giá, kiểm BR-QUY-02/03, cộng/gỡ
số đã trả, gộp nguồn hóa đơn còn nợ) sống ở `ket.posting.settlements` — chứng
từ tiền gửi (module `bank`) dùng cùng cơ chế mà không import được module này.
Ở đây chỉ còn phần buộc vào bảng `cash_settlements` và hình dạng payload phiếu.

Ba việc, ba thời điểm — xem docstring `ket.posting.settlements`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.modules.cash_book.models import CashSettlement
from ket.modules.cash_book.schemas import CashVoucherIn
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
    "open_invoices",
    "price_settlements",
    "revert_settlements",
]


def price_settlements(
    session: Session, payload: CashVoucherIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một phiếu (BR-QUY-02/03)."""
    return price_settlement_inputs(
        session,
        settlements=payload.settlements,
        lines_total_fc=sum((line.amount_fc for line in payload.lines), Decimal(0)),
        partner_id=payload.partner_id,
        partner_kind=payload.partner_kind,
        branch_id=payload.branch_id,
        currency_code=payload.currency_code,
        exchange_rate=payload.exchange_rate,
        scale=scale,
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
