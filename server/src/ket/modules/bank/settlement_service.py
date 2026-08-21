"""Đối trừ công nợ của chứng từ tiền gửi (FR-BNK-007, FR-SYS-066) — lát 6C.

Lớp vỏ buộc `ket.posting.settlements` (cơ chế dùng chung với phiếu quỹ) vào
bảng `bank_settlements` và hình dạng payload chứng từ tiền gửi. Toàn bộ luật
(BR-QUY-02/03, định giá, phần lẻ làm tròn dồn lát cuối) sống bên đó.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.modules.bank.models import BankSettlement
from ket.modules.bank.schemas import BankVoucherIn
from ket.posting.settlements import (
    PricedSettlement,
    apply_settlement_rows,
    open_invoices,
    revert_settlement_rows,
)
from ket.posting.settlements import (
    price_settlements as price_settlement_inputs,
)

__all__ = [
    "PricedSettlement",
    "apply_settlements",
    "open_invoices",
    "price_settlements",
    "revert_settlements",
]


def price_settlements(
    session: Session, payload: BankVoucherIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một chứng từ tiền gửi."""
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


def _settlements_of(session: Session, voucher_id: UUID) -> Sequence[BankSettlement]:
    return (
        session.execute(select(BankSettlement).where(BankSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã trả vào từng đích — chạy SAU `PostingService.post`, cùng transaction."""
    apply_settlement_rows(session, _settlements_of(session, voucher_id))


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ số đã trả — chạy SAU `PostingService.unpost`, cùng transaction."""
    revert_settlement_rows(session, _settlements_of(session, voucher_id))
