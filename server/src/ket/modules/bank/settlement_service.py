"""Đối trừ công nợ của chứng từ tiền gửi (FR-BNK-007, FR-SYS-066) — lát 6C.

Lớp vỏ buộc `ket.posting.settlements` (cơ chế dùng chung với phiếu quỹ) vào
bảng `bank_settlements` và hình dạng payload chứng từ tiền gửi. Toàn bộ luật
(BR-QUY-02/03, định giá, phần lẻ làm tròn dồn lát cuối) sống bên đó.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.modules.bank.models import (
    MONEY_IN_BY_KIND,
    BankSettlement,
    BankVoucherLine,
)
from ket.modules.bank.schemas import BankVoucherIn
from ket.posting.debt_lines import (
    money_voucher_settles_advance,
    record_pair_voucher_debt,
    remove_voucher_debt,
    settlement_scope_of,
    sides_of_payload_lines,
)
from ket.posting.documents.models import Voucher
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
    # Phạm vi đo theo DÒNG CÔNG NỢ, không theo tổng tiền chứng từ (lát 7C-5,
    # điều kiện #9): khối đối trừ nhận đối tác ở header còn sổ cái ghi theo đối
    # tác từng dòng, nên chỉ tổng dòng công nợ mới là thứ hai vế cùng nhích.
    settles_advance = money_voucher_settles_advance(
        money_in=MONEY_IN_BY_KIND.get(payload.kind, False),
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
        # Ủy nhiệm chi cho khách và giấy báo có của người bán tất toán khoản
        # ỨNG TRƯỚC, không phải khoản nợ (lát 7C-4) — cùng luật phiếu thu/chi,
        # và cùng giá trị ấy khóa CHIỀU của dòng công nợ ở `settlement_scope_of`.
        settles_advance=settles_advance,
    )


def sync_subledger_after_post(session: Session, voucher_id: UUID, *, scale: int) -> None:
    """Ghi khoản công nợ chứng từ sinh ra vào sổ phụ — SAU `PostingService.post`.

    Giấy báo có Có 131 mang đối tác mà không chọn đối trừ là khoản khách **ứng
    trước**: nó nhích số dư 131 trên sổ cái, nên phải nhích cả sổ phụ, nếu
    không `arap_matches_control` đỏ trên dữ liệu ĐÚNG (điều kiện #2, mở từ 7A).
    """
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - engine vừa ghi sổ chính chứng từ này
        raise RuntimeError(f"Không tìm thấy chứng từ tiền gửi {voucher_id} để ghi sổ phụ")
    # Tiền tệ và tỷ giá đọc từ HEADER — xem chú thích cùng chỗ ở `cash_book`.
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
    """Gỡ dòng sổ phụ của chứng từ — chạy SAU `PostingService.unpost`."""
    remove_voucher_debt(session, voucher_id=voucher_id)


def _lines_of(session: Session, voucher_id: UUID) -> Sequence[BankVoucherLine]:
    return (
        session.execute(
            select(BankVoucherLine)
            .where(BankVoucherLine.voucher_id == voucher_id)
            .order_by(BankVoucherLine.line_no)
        )
        .scalars()
        .all()
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
