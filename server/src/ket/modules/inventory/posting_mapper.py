"""Dịch phiếu kho → `PostingRequest` (hợp đồng một chiều với engine).

Một dòng phiếu sinh **một cặp** Nợ/Có khi và chỉ khi nó có cặp TK **và** giá
đã biết — từ một trong hai nguồn:

* `amount_fc` trên dòng (phiếu nhập gõ tay, nhập từ hóa đơn mua): cặp theo
  nguyên tệ + tỷ giá của phiếu, như mọi chứng từ khác;
* **movement đã tính giá** của dòng (phiếu xuất, vế **đi** của phiếu chuyển —
  lát 8B): cặp bằng `amount` VND, `currency = VND, rate = 1`. Giá vốn là số sổ
  cái, không có nguyên tệ ở sổ kho; phiếu xuất USD sinh từ hóa đơn bán vẫn cân
  vì `check_balanced` cân theo từng `(sổ, tiền tệ)`.

Ba trường hợp không sinh gì, đều có chủ đích: dòng không có cặp TK (phiếu nhập
từ hóa đơn mua — Nợ 156 / Có 331 đã ở hóa đơn; chuyển kho nội bộ — BR-STK-06);
dòng có cặp TK mà movement còn chờ giá (phiếu xuất vừa ghi sổ, nhập hàng bán
trả lại FR-STK-004 8C); dòng chưa có movement (lượt ghi sổ đầu — movement dựng
ở hook `after_post`, SAU khi mapper chạy).

Hệ quả: một phiếu có thể ghi sổ với **0 dòng GL** — `check_balanced` tính
`zero_amount` theo từng sổ nên tập rỗng không phải vi phạm; bài kiểm của 8A
ghim điều này vì nó là điều kiện tiên quyết của cả thiết kế "giá vốn tính sau".
Engine 8B tính xong thì gọi `PostingService.repost` với chính `build_posting_
request` này — một mapper cho cả lượt ghi đầu lẫn mọi lượt ghi lại.

Chiều phân tích gắn cả hai bên (cùng luật "dòng không chạm quỹ" của mapper
phiếu thu/chi): TK kho đòi `item;warehouse`, TK đối ứng (154/621/632…) đòi
`cost_object`/`project` — không có cơ sở chọn bên, và thừa chiều trên TK không
theo dõi là vô hại còn thiếu thì validator chặn.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.inventory.models import (
    CostState,
    InventoryMovement,
    InventoryVoucher,
    InventoryVoucherLine,
    MovementDirection,
)
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)

__all__ = ["build_posting_request", "build_posting_requests", "line_dimensions"]


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Callable đăng ký vào `POSTING_DOCUMENT_REGISTRY` cho cả NK/XK/CK."""
    return build_posting_requests(session, [voucher_id])[voucher_id]


def build_posting_requests(
    session: Session, voucher_ids: Sequence[UUID]
) -> dict[UUID, PostingRequest]:
    """Cùng mapper cho một LÔ phiếu — bốn câu đọc cho cả lô thay vì bốn câu
    mỗi phiếu (engine tính giá ghi lại giá vốn hàng nghìn phiếu một lượt)."""
    if not voucher_ids:
        return {}
    ids = list(voucher_ids)
    vouchers = {
        voucher.id: voucher
        for voucher in session.execute(select(Voucher).where(Voucher.id.in_(ids))).scalars()
    }
    bodies = {
        body.id: body
        for body in session.execute(
            select(InventoryVoucher).where(InventoryVoucher.id.in_(ids))
        ).scalars()
    }
    lines_by_voucher: dict[UUID, list[InventoryVoucherLine]] = {
        voucher_id: [] for voucher_id in ids
    }
    for line in session.execute(
        select(InventoryVoucherLine)
        .where(InventoryVoucherLine.voucher_id.in_(ids))
        .order_by(InventoryVoucherLine.voucher_id, InventoryVoucherLine.line_no)
    ).scalars():
        lines_by_voucher[line.voucher_id].append(line)
    costed_amounts = _costed_out_amounts(session, ids)
    currencies: dict[date, str] = {}
    requests: dict[UUID, PostingRequest] = {}
    for voucher_id in ids:
        voucher = vouchers.get(voucher_id)
        body = bodies.get(voucher_id)
        if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
            raise RuntimeError(f"Phiếu kho {voucher_id} thiếu header hoặc thân")
        if voucher.posting_date not in currencies:
            year = fiscal_year_covering(session, voucher.posting_date)
            if year is None:  # pragma: no cover - chứng từ đã có kỳ thì có năm
                raise RuntimeError(
                    f"Ngày ghi sổ {voucher.posting_date} không thuộc năm tài chính nào"
                )
            currencies[voucher.posting_date] = year.base_currency
        posting_lines: list[PostingLine] = []
        for line in lines_by_voucher[voucher_id]:
            posting_lines.extend(
                _pair_of(
                    voucher,
                    line,
                    costed_amounts.get(line.id),
                    cost_currency=currencies[voucher.posting_date],
                )
            )
        requests[voucher_id] = PostingRequest(
            voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
        )
    return requests


def _costed_out_amounts(session: Session, voucher_ids: Sequence[UUID]) -> dict[UUID, Decimal]:
    """`amount` VND của movement đã tính giá **do engine quyết**, theo dòng phiếu:
    chiều xuất (vế đi của phiếu chuyển là vế mang giá vốn — vế đến nhận cùng
    giá, không bút toán) và chiều nhập lấy giá từ lần xuất (hàng bán trả lại,
    FR-STK-004 8C-1 — Nợ 156 / Có 632 theo giá engine chép)."""
    rows = session.execute(
        select(InventoryMovement.line_id, InventoryMovement.amount).where(
            InventoryMovement.voucher_id.in_(list(voucher_ids)),
            InventoryMovement.cost_state == CostState.COSTED,
            or_(
                InventoryMovement.direction == MovementDirection.OUT,
                InventoryMovement.source_movement_id.is_not(None),
            ),
        )
    ).all()
    return {line_id: amount for line_id, amount in rows if amount is not None}


def _pair_of(
    voucher: Voucher,
    line: InventoryVoucherLine,
    costed_amount: Decimal | None,
    *,
    cost_currency: str,
) -> list[PostingLine]:
    if line.debit_account_id is None or line.credit_account_id is None:
        return []
    if line.amount_fc is not None:
        amount, currency, rate = line.amount_fc, voucher.currency_code, voucher.exchange_rate
    elif costed_amount is not None:
        # Giá vốn là số sổ cái: đồng tiền hạch toán của năm, tỷ giá 1.
        amount, currency, rate = costed_amount, cost_currency, Decimal(1)
    else:
        return []
    if amount == 0:
        return []
    dimensions = line_dimensions(line)
    description = line.description or voucher.description
    return [
        PostingLine(
            account_id=line.debit_account_id,
            corresponding_account_id=line.credit_account_id,
            debit_fc=amount,
            credit_fc=Decimal(0),
            currency=currency,
            rate=rate,
            dimensions=dimensions,
            source_line_id=line.id,
            description=description,
        ),
        PostingLine(
            account_id=line.credit_account_id,
            corresponding_account_id=line.debit_account_id,
            debit_fc=Decimal(0),
            credit_fc=amount,
            currency=currency,
            rate=rate,
            dimensions=dimensions,
            source_line_id=line.id,
            description=description,
        ),
    ]


def line_dimensions(line: InventoryVoucherLine) -> PostingDimensions:
    extended = tuple(
        ExtendedDimensionValue(dimension_id=int(dimension_id), value_id=value_id)
        for dimension_id, value_id in sorted((line.extended_dimensions or {}).items())
    )
    return PostingDimensions(
        partner_id=line.partner_id,
        partner_kind=(PartnerKind(line.partner_kind) if line.partner_kind is not None else None),
        cost_object_id=line.cost_object_id,
        project_id=line.project_id,
        order_id=line.order_id,
        contract_id=line.contract_id,
        expense_item_id=line.expense_item_id,
        item_id=line.item_id,
        warehouse_id=line.warehouse_id,
        extended=extended,
    )
