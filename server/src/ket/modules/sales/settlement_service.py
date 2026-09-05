"""Đối trừ của chứng từ TRẢ LẠI / GIẢM GIÁ HÀNG BÁN vào hóa đơn gốc (SRS 06 §3, lát 7C-2).

Quyết định user 2026-09-04, cùng hình dạng D1 của 7B cho hàng mua trả lại: hai
loại chứng từ giảm trừ **không sinh một khoản công nợ âm** trên sổ phụ
(`ar_ap_ledger` không có dòng âm — CHECK `amounts_not_negative`). Khoản giảm
trừ là một lượt **giảm nợ** của hóa đơn gốc, và giảm nợ đi qua đúng cơ chế mà
phiếu thu dùng: dòng đối trừ định giá lúc cất, cộng vào `settled` lúc ghi sổ,
gỡ ra lúc bỏ ghi sổ, chênh lệch tỷ giá vào 515/635.

Hóa đơn gốc **đã thu đủ** thì không còn gì để đối trừ: `open_invoices` không
liệt kê nó và `price_settlements` từ chối dòng trỏ vào nó
(`SETTLEMENT_OVERPAID_CODE`). Đường đúng lúc ấy là trả tiền lại khách bằng
phiếu chi — cùng lựa chọn với chiều mua, và nó giữ cho `ar_ap_ledger` chỉ có
đúng một chiều cho mỗi `target_kind`.

Phần không-phụ-thuộc-module sống ở `ket.posting.settlements` (7B đã tách); ở
đây chỉ còn phần buộc vào bảng `sales_settlements` và hình dạng payload.
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
from ket.modules.sales.models import SalesSettlement
from ket.modules.sales.schemas import SalesInvoiceIn
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
    "invoice_total_fc",
    "open_invoices",
    "price_settlements",
    "revert_settlements",
]


def invoice_total_fc(payload: SalesInvoiceIn) -> Decimal:
    """Tổng tiền hóa đơn = doanh thu sau chiết khấu + thuế GTGT đầu ra.

    Đây là số ở ô "Tổng cộng" của chứng từ, cũng là số phải thu khách hàng, và
    với chứng từ giảm trừ là số mà tổng đối trừ phải khớp (BR-QUY-03 áp sang
    chứng từ trả lại / giảm giá).
    """
    return sum((line.amount_fc + line.vat_amount_fc for line in payload.lines), Decimal(0))


def _ledger_total(payload: SalesInvoiceIn, *, scale: int) -> Decimal:
    """VND mà bút toán ghi Có TK phải thu — cộng từ **từng mẩu đã quy đổi**.

    Bộ ghi sổ quy đổi mỗi `PostingLine` riêng, và chứng từ giảm trừ chẻ thành
    một cặp tiền hàng + một cặp thuế cho mỗi dòng (`posting_mapper`), nên đây
    là tổng phải khớp — không phải `round(tổng nguyên tệ × tỷ giá)`.

    Chứng từ bán không có khoản nào ngoài dòng hàng đâm vào TK công nợ (khác
    hóa đơn mua, nơi khoản chi phí mua hàng có thể nợ một NCC khác), nên tổng
    này chỉ cộng dòng. Thêm bất kỳ khoản nào khác ghi lên `receivable_account_id`
    thì hàm này phải cộng cả phần ấy, nếu không phần lẻ dồn vào dòng cuối sẽ là
    cả khoản đó chứ không phải vài đồng làm tròn.
    """
    rate = payload.exchange_rate
    total = Decimal(0)
    for line in payload.lines:
        total += convert_currency(line.amount_fc, rate, scale)
        if line.vat_amount_fc > 0 and line.vat_account_id is not None:
            total += convert_currency(line.vat_amount_fc, rate, scale)
    return total


def price_settlements(
    session: Session, payload: SalesInvoiceIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một chứng từ giảm trừ.

    Dòng cuối gánh phần lẻ để **tổng VND của các dòng đối trừ bằng đúng số ghi
    Có TK phải thu của các cặp hàng/thuế** — và vì cặp bù chênh lệch tỷ giá
    dịch TK ấy đi đúng `−Σ fx_diff`, số Có RÒNG còn lại bằng đúng `Σ settled`.
    Hai bên làm tròn theo hai cách chẻ khác nhau — sổ cái theo từng dòng
    hàng/thuế, đối trừ theo từng hóa đơn đích — nên chứng từ ngoại tệ lệch vài
    đồng, và hóa đơn gốc sẽ treo mãi phần lệch ấy nếu không dồn. Phần lẻ vào cả
    `amount` lẫn `fx_diff` nên `settled` (= `amount − fx_diff`, giá trị ghi
    nhận đã giải phóng) không đổi.
    """
    priced = price_settlement_inputs(
        session,
        settlements=payload.settlements,
        lines_total_fc=invoice_total_fc(payload),
        partner_id=payload.customer_id,
        partner_kind=PartnerKind.CUSTOMER,
        branch_id=payload.branch_id,
        currency_code=payload.currency_code,
        exchange_rate=payload.exchange_rate,
        scale=scale,
        # Chứng từ giảm trừ ghi Có `receivable_account_id`, nên hóa đơn gốc
        # phải đang treo nợ trên đúng TK ấy — khác TK là sổ cái giảm một TK,
        # sổ phụ giảm TK kia.
        account_id=payload.receivable_account_id,
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


def _settlements_of(session: Session, voucher_id: UUID) -> Sequence[SalesSettlement]:
    return (
        session.execute(select(SalesSettlement).where(SalesSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã giảm vào hóa đơn gốc — chạy SAU `PostingService.post`, cùng transaction."""
    apply_settlement_rows(session, _settlements_of(session, voucher_id))


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ số đã giảm — chạy SAU `PostingService.unpost`, cùng transaction."""
    revert_settlement_rows(session, _settlements_of(session, voucher_id))
