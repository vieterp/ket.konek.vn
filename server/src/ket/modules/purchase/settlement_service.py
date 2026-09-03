"""Đối trừ của chứng từ TRẢ LẠI HÀNG MUA vào hóa đơn gốc (SRS 05 §3, lát 7B).

Quyết định thiết kế 7B: trả lại hàng **không sinh một khoản công nợ âm** trên
sổ phụ (`ar_ap_ledger` không có dòng âm — CHECK `amounts_not_negative`). Khoản
trả lại là một lượt **giảm nợ** của hóa đơn gốc, và giảm nợ đi qua đúng cơ
chế mà phiếu chi dùng: dòng đối trừ định giá lúc cất, cộng vào `settled` lúc
ghi sổ, gỡ ra lúc bỏ ghi sổ, chênh lệch tỷ giá vào 515/635. Phần không-phụ-
thuộc-module sống ở `ket.posting.settlements`; ở đây chỉ còn phần buộc vào
bảng `purchase_settlements` và hình dạng payload hóa đơn.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.money import convert_currency
from ket.modules.purchase.models import PurchaseSettlement
from ket.modules.purchase.schemas import PurchaseInvoiceIn
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


def invoice_total_fc(payload: PurchaseInvoiceIn) -> Decimal:
    """Tổng tiền hóa đơn = hàng + thuế của dòng + chi phí mua hàng (gốc + thuế).

    Đây là số ở ô "Tổng cộng" của chứng từ, và với chứng từ trả lại là số mà
    tổng đối trừ phải khớp (BR-QUY-03 áp sang chứng từ trả hàng).
    """
    lines = sum((line.amount_fc + line.vat_amount_fc for line in payload.lines), Decimal(0))
    costs = sum((cost.amount_fc + cost.vat_amount_fc for cost in payload.landed_costs), Decimal(0))
    return lines + costs


def _ledger_total(payload: PurchaseInvoiceIn, *, scale: int) -> Decimal:
    """VND mà bút toán ghi Nợ TK phải trả — cộng từ **từng mẩu đã quy đổi**.

    Bộ ghi sổ quy đổi mỗi `PostingLine` riêng, và chứng từ trả lại chẻ thành
    một cặp tiền hàng + một cặp thuế cho mỗi dòng (`posting_mapper`), nên đây
    là tổng phải khớp — không phải `round(tổng nguyên tệ × tỷ giá)`.

    **Chỉ cộng dòng hàng vì `_invoice_sane` (schemas) cấm chi phí mua hàng trên
    chứng từ trả lại — loại chứng từ nào sau này vừa có đối trừ vừa có chi phí
    mua hàng thì hàm này phải cộng cả phần chi phí**, nếu không phần lẻ dồn vào
    dòng cuối sẽ là cả khoản chi phí chứ không phải vài đồng làm tròn.
    """
    rate = payload.exchange_rate
    total = Decimal(0)
    for line in payload.lines:
        total += convert_currency(line.amount_fc, rate, scale)
        if line.vat_amount_fc > 0 and line.vat_account_id is not None:
            total += convert_currency(line.vat_amount_fc, rate, scale)
    return total


def price_settlements(
    session: Session, payload: PurchaseInvoiceIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một chứng từ trả lại.

    Dòng cuối gánh phần lẻ để **tổng VND của các dòng đối trừ bằng đúng số
    ghi Nợ TK phải trả của các cặp hàng/thuế** — và vì cặp bù chênh lệch tỷ giá
    dịch TK ấy đi đúng `−Σ fx_diff`, số Nợ RÒNG còn lại bằng đúng `Σ settled`. Hai bên làm tròn theo hai cách chẻ khác
    nhau — sổ cái theo từng dòng hàng/thuế, đối trừ theo từng hóa đơn đích —
    nên chứng từ ngoại tệ lệch vài đồng: dòng bù chênh lệch tỷ giá đưa TK
    phải trả về `amount − fx_diff` mỗi dòng, và số ấy phải cộng lại đúng bằng
    số sổ phụ trừ đi, nếu không hóa đơn gốc treo mãi vài đồng không ai đối trừ
    được. Phần lẻ vào cả `amount` lẫn `fx_diff` nên `settled` (= `amount −
    fx_diff`, giá trị ghi nhận đã giải phóng) không đổi.
    """
    priced = price_settlement_inputs(
        session,
        settlements=payload.settlements,
        lines_total_fc=invoice_total_fc(payload),
        partner_id=payload.vendor_id,
        partner_kind=PartnerKind.VENDOR,
        branch_id=payload.branch_id,
        currency_code=payload.currency_code,
        exchange_rate=payload.exchange_rate,
        scale=scale,
        # Chứng từ trả lại ghi Nợ `payable_account_id`, nên hóa đơn gốc phải
        # đang treo nợ trên đúng TK ấy — khác TK là sổ cái giảm một TK, sổ phụ
        # giảm TK kia.
        account_id=payload.payable_account_id,
    )
    if not priced:
        return priced
    remainder = _ledger_total(payload, scale=scale) - sum(
        (row.amount for row in priced), Decimal(0)
    )
    if remainder:
        last = priced[-1]
        priced[-1] = replace(last, amount=last.amount + remainder, fx_diff=last.fx_diff + remainder)
    return priced


def _settlements_of(session: Session, voucher_id: UUID) -> Sequence[PurchaseSettlement]:
    return (
        session.execute(
            select(PurchaseSettlement).where(PurchaseSettlement.voucher_id == voucher_id)
        )
        .scalars()
        .all()
    )


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã giảm vào hóa đơn gốc — chạy SAU `PostingService.post`, cùng transaction."""
    apply_settlement_rows(session, _settlements_of(session, voucher_id))


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ số đã giảm — chạy SAU `PostingService.unpost`, cùng transaction."""
    revert_settlement_rows(session, _settlements_of(session, voucher_id))
