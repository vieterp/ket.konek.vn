"""Dịch hóa đơn bán hàng → `PostingRequest` (hợp đồng một chiều với engine).

Mỗi dòng hóa đơn trải thành các **cặp** Nợ/Có cùng số tiền, TK bên này làm đối
ứng của bên kia — cùng khuôn mapper hóa đơn mua, đảo hai vế. Hai sổ ghi giống
nhau nên `management_lines=None`. Với hóa đơn bán thường (kind 0, 1, 4):

* Hàng/dịch vụ: Nợ `receivable_account_id` / Có `line.account_id`, số
  `amount_fc` — **đã trừ chiết khấu thương mại**. Chiết khấu ghi giảm doanh thu
  ngay trên dòng (SRS 06 §3.2 "trừ trực tiếp trên hóa đơn"), nên không có cặp
  521 riêng và `discount_amount_fc` không đi vào bút toán nào.
* Thuế GTGT đầu ra: Nợ `receivable_account_id` / Có `line.vat_account_id`.

**Trả lại hàng bán (kind 2) và giảm giá hàng bán (kind 3)** đảo chiều mọi cặp
(Nợ 521/511 / Có 131, Nợ 33311 / Có 131), và thêm cặp chênh lệch tỷ giá của
lượt đối trừ vào hóa đơn gốc — `money_in=True` vì đây là một lượt **giảm phải
thu**, cùng hướng lãi/lỗ với phiếu thu (xem `posting.settlements._is_gain`).
Đối xứng với chứng từ trả lại hàng mua của 7B, nơi cùng lập luận cho ra `False`.

**Không có cặp giá vốn.** Nợ 632 / Có 156 đòi giá xuất kho, mà giá xuất kho là
việc của phase 8 — `sales_invoices.cogs_posted` ở lại `false` cho tới lúc ấy.
Ba cột `cogs_account_id` / `inventory_account_id` / `unit_cost_fc` trên dòng là
dữ liệu phase 8 đọc, không phải dữ liệu mapper này ghi.

Chiều phân tích: khách hàng gắn vào bên công nợ (131 của hóa đơn); vật tư/kho/
khoản mục/vụ việc gắn vào bên doanh thu. Bên thuế GTGT đầu ra nhận chiều của
dòng hàng — vô hại vì 33311 không bật theo dõi chiều nào.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.modules.sales.models import (
    REVERSING_KINDS,
    SalesInvoice,
    SalesInvoiceLine,
    SalesSettlement,
)
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)
from ket.posting.settlements import fx_adjustment_lines

_ZERO = Decimal(0)


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Đọc chi tiết đã lưu và dựng yêu cầu ghi sổ — callable đăng ký vào
    `POSTING_DOCUMENT_REGISTRY` cho loại `SAL`."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(SalesInvoice, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
        raise RuntimeError(f"Hóa đơn bán {voucher_id} thiếu header hoặc thân")
    lines = (
        session.execute(
            select(SalesInvoiceLine)
            .where(SalesInvoiceLine.voucher_id == voucher_id)
            .order_by(SalesInvoiceLine.line_no)
        )
        .scalars()
        .all()
    )
    settlements = (
        session.execute(select(SalesSettlement).where(SalesSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )

    mapper = _Mapper(voucher, body)
    posting_lines: list[PostingLine] = []
    for line in lines:
        posting_lines.extend(mapper.revenue_pairs(line))
    if body.kind in REVERSING_KINDS:
        posting_lines.extend(
            fx_adjustment_lines(session, voucher, money_in=True, settlements=settlements)
        )
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


class _Mapper:
    def __init__(self, voucher: Voucher, body: SalesInvoice) -> None:
        self._voucher = voucher
        self._body = body
        self._reversed = body.kind in REVERSING_KINDS
        self._customer = PostingDimensions(
            partner_id=body.customer_id, partner_kind=PartnerKind.CUSTOMER
        )

    def revenue_pairs(self, line: SalesInvoiceLine) -> list[PostingLine]:
        """Cặp doanh thu + cặp thuế của một dòng, đối ứng với TK phải thu."""
        dimensions = _line_dimensions(line)
        description = line.description or self._voucher.description
        pairs = self._pair(
            credit_account=line.account_id,
            credit_dimensions=dimensions,
            amount_fc=line.amount_fc,
            source_line_id=line.id,
            description=description,
        )
        if line.vat_amount_fc > _ZERO and line.vat_account_id is not None:
            pairs.extend(
                self._pair(
                    credit_account=line.vat_account_id,
                    credit_dimensions=dimensions,
                    amount_fc=line.vat_amount_fc,
                    source_line_id=line.id,
                    description=description,
                )
            )
        return pairs

    def _pair(
        self,
        *,
        credit_account: int,
        credit_dimensions: PostingDimensions,
        amount_fc: Decimal,
        source_line_id: UUID,
        description: str | None,
    ) -> list[PostingLine]:
        """Một cặp Nợ phải thu / Có doanh thu; chứng từ giảm trừ đảo chiều cả cặp."""
        debit_account = self._body.receivable_account_id
        return [
            self._line(
                account_id=debit_account,
                corresponding=credit_account,
                amount_fc=amount_fc,
                credit=self._reversed,
                dimensions=self._customer,
                source_line_id=source_line_id,
                description=description,
            ),
            self._line(
                account_id=credit_account,
                corresponding=debit_account,
                amount_fc=amount_fc,
                credit=not self._reversed,
                dimensions=credit_dimensions,
                source_line_id=source_line_id,
                description=description,
            ),
        ]

    def _line(
        self,
        *,
        account_id: int,
        corresponding: int | None,
        amount_fc: Decimal,
        credit: bool,
        dimensions: PostingDimensions,
        source_line_id: UUID,
        description: str | None,
    ) -> PostingLine:
        return PostingLine(
            account_id=account_id,
            corresponding_account_id=corresponding,
            debit_fc=_ZERO if credit else amount_fc,
            credit_fc=amount_fc if credit else _ZERO,
            currency=self._voucher.currency_code,
            rate=self._voucher.exchange_rate,
            dimensions=dimensions,
            source_line_id=source_line_id,
            description=description,
        )


def _line_dimensions(line: SalesInvoiceLine) -> PostingDimensions:
    extended = tuple(
        ExtendedDimensionValue(dimension_id=int(dimension_id), value_id=value_id)
        for dimension_id, value_id in sorted((line.extended_dimensions or {}).items())
    )
    return PostingDimensions(
        cost_object_id=line.cost_object_id,
        project_id=line.project_id,
        order_id=line.order_id,
        contract_id=line.contract_id,
        expense_item_id=line.expense_item_id,
        item_id=line.item_id,
        warehouse_id=line.warehouse_id,
        extended=extended,
    )
