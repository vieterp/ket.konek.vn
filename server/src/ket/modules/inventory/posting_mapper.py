"""Dịch phiếu kho → `PostingRequest` (hợp đồng một chiều với engine).

Một dòng phiếu sinh **một cặp** Nợ/Có khi và chỉ khi nó có cặp TK **và** giá
đã biết (`amount_fc` ≠ NULL). Ba trường hợp không sinh gì, đều có chủ đích:

* dòng không có cặp TK — phiếu nhập từ hóa đơn mua (Nợ 156 / Có 331 đã ở hóa
  đơn), chuyển kho nội bộ (không đổi tổng giá trị, BR-STK-06);
* dòng có cặp TK mà chưa có giá — phiếu xuất (giá vốn do engine 8B tính rồi
  repost), phiếu nhập hàng bán trả lại (FR-STK-004, 8C);
* phiếu xuất bán sinh từ hóa đơn: cặp 632/156 nằm trên dòng, chờ giá.

Hệ quả: một phiếu có thể ghi sổ với **0 dòng GL** — `check_balanced` tính
`zero_amount` theo từng sổ nên tập rỗng không phải vi phạm; bài kiểm của lát
ghim điều này vì nó là điều kiện tiên quyết của cả thiết kế "giá vốn tính sau".

Chiều phân tích gắn cả hai bên (cùng luật "dòng không chạm quỹ" của mapper
phiếu thu/chi): TK kho đòi `item;warehouse`, TK đối ứng (154/621/632…) đòi
`cost_object`/`project` — không có cơ sở chọn bên, và thừa chiều trên TK không
theo dõi là vô hại còn thiếu thì validator chặn.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.modules.inventory.models import InventoryVoucher, InventoryVoucherLine
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)

__all__ = ["build_posting_request", "line_dimensions"]


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Callable đăng ký vào `POSTING_DOCUMENT_REGISTRY` cho cả NK/XK/CK."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(InventoryVoucher, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
        raise RuntimeError(f"Phiếu kho {voucher_id} thiếu header hoặc thân")
    lines = (
        session.execute(
            select(InventoryVoucherLine)
            .where(InventoryVoucherLine.voucher_id == voucher_id)
            .order_by(InventoryVoucherLine.line_no)
        )
        .scalars()
        .all()
    )
    posting_lines: list[PostingLine] = []
    for line in lines:
        posting_lines.extend(_pair_of(voucher, line))
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


def _pair_of(voucher: Voucher, line: InventoryVoucherLine) -> list[PostingLine]:
    if (
        line.debit_account_id is None
        or line.credit_account_id is None
        or line.amount_fc is None
        or line.amount_fc == 0
    ):
        return []
    dimensions = line_dimensions(line)
    description = line.description or voucher.description
    return [
        PostingLine(
            account_id=line.debit_account_id,
            corresponding_account_id=line.credit_account_id,
            debit_fc=line.amount_fc,
            credit_fc=Decimal(0),
            currency=voucher.currency_code,
            rate=voucher.exchange_rate,
            dimensions=dimensions,
            source_line_id=line.id,
            description=description,
        ),
        PostingLine(
            account_id=line.credit_account_id,
            corresponding_account_id=line.debit_account_id,
            debit_fc=Decimal(0),
            credit_fc=line.amount_fc,
            currency=voucher.currency_code,
            rate=voucher.exchange_rate,
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
