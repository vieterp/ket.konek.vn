"""Dịch hóa đơn mua hàng → `PostingRequest` (hợp đồng một chiều với engine).

Mỗi dòng hóa đơn trải thành các **cặp** Nợ/Có cùng số tiền, TK bên này làm đối
ứng của bên kia — cùng khuôn với mapper phiếu thu/chi, và hai sổ ghi giống
nhau nên `management_lines=None`. Với hóa đơn mua thường (kind 0..3):

* Hàng/dịch vụ: Nợ `line.account_id` / Có `payable_account_id`, số `amount_fc`.
* Thuế GTGT của dòng: Nợ `line.vat_account_id` / Có `payable_account_id`.
* Chi phí mua hàng: **cũng là cặp** — mỗi mẩu (khoản chi phí, dòng hàng) từ
  `landed_cost.pair_costs_with_lines` thành một cặp Nợ `line.account_id` / Có
  `credit_account_id` của khoản. Không được ghi "một dòng Có tổng khoản + N
  dòng Nợ phần phân bổ": engine quy đổi từng dòng rồi mới kiểm cân, nên hình
  ấy chỉ cân khi tỷ giá nguyên — với tỷ giá lẻ nó bị `posting.unbalanced`.
* Thuế GTGT của khoản chi phí: cặp Nợ `vat_account_id` / Có `credit_account_id`.

**Trả lại hàng mua** (kind 4) đảo chiều mọi cặp (Nợ 331 / Có 156, Nợ 331 /
Có 1331), không có chi phí mua hàng, và thêm cặp chênh lệch tỷ giá của lượt
đối trừ vào hóa đơn gốc — `money_in=False` vì đây là một lượt **giảm phải
trả**, cùng hướng lãi/lỗ với phiếu chi (xem `posting.settlements._is_gain`).

Chiều phân tích: đối tác gắn vào bên công nợ (331 của hóa đơn, TK Có của
khoản chi phí có `vendor_id`); vật tư/kho/khoản mục/vụ việc gắn vào bên hàng.
Bên thuế GTGT nhận chiều của dòng hàng — vô hại vì `1331` không bật theo dõi
chiều nào (gói builtin bỏ theo dõi `item` ở migration 0026: thuế đầu vào là sự
thật theo hóa đơn, không theo vật tư).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.modules.purchase.landed_cost import pair_costs_with_lines
from ket.modules.purchase.models import (
    LandedCost,
    PurchaseInvoice,
    PurchaseInvoiceKind,
    PurchaseInvoiceLine,
    PurchaseSettlement,
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
_EMPTY_DIMENSIONS = PostingDimensions()


def build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Đọc chi tiết đã lưu và dựng yêu cầu ghi sổ — callable đăng ký vào
    `POSTING_DOCUMENT_REGISTRY` cho loại `PUR`."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(PurchaseInvoice, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - FK một-một bảo đảm
        raise RuntimeError(f"Hóa đơn mua {voucher_id} thiếu header hoặc thân")
    lines = (
        session.execute(
            select(PurchaseInvoiceLine)
            .where(PurchaseInvoiceLine.voucher_id == voucher_id)
            .order_by(PurchaseInvoiceLine.line_no)
        )
        .scalars()
        .all()
    )
    costs = (
        session.execute(
            select(LandedCost)
            .where(LandedCost.voucher_id == voucher_id)
            .order_by(LandedCost.line_no)
        )
        .scalars()
        .all()
    )
    settlements = (
        session.execute(
            select(PurchaseSettlement).where(PurchaseSettlement.voucher_id == voucher_id)
        )
        .scalars()
        .all()
    )

    mapper = _Mapper(voucher, body)
    posting_lines: list[PostingLine] = []
    for line in lines:
        posting_lines.extend(mapper.goods_pairs(line))
    posting_lines.extend(mapper.landed_cost_lines(lines, costs))
    if body.kind == PurchaseInvoiceKind.RETURN:
        posting_lines.extend(
            fx_adjustment_lines(session, voucher, money_in=False, settlements=settlements)
        )
    return PostingRequest(
        voucher_id=voucher_id, financial_lines=tuple(posting_lines), management_lines=None
    )


class _Mapper:
    def __init__(self, voucher: Voucher, body: PurchaseInvoice) -> None:
        self._voucher = voucher
        self._body = body
        self._reversed = body.kind == PurchaseInvoiceKind.RETURN
        self._vendor = PostingDimensions(partner_id=body.vendor_id, partner_kind=PartnerKind.VENDOR)

    def goods_pairs(self, line: PurchaseInvoiceLine) -> list[PostingLine]:
        """Cặp hàng + cặp thuế của một dòng, đối ứng với TK phải trả của hóa đơn."""
        dimensions = _line_dimensions(line)
        description = line.description or self._voucher.description
        pairs = self._pair(
            debit_account=line.account_id,
            debit_dimensions=dimensions,
            credit_account=self._body.payable_account_id,
            credit_dimensions=self._vendor,
            amount_fc=line.amount_fc,
            source_line_id=line.id,
            description=description,
        )
        if line.vat_amount_fc > _ZERO and line.vat_account_id is not None:
            pairs.extend(
                self._pair(
                    debit_account=line.vat_account_id,
                    debit_dimensions=dimensions,
                    credit_account=self._body.payable_account_id,
                    credit_dimensions=self._vendor,
                    amount_fc=line.vat_amount_fc,
                    source_line_id=line.id,
                    description=description,
                )
            )
        return pairs

    def landed_cost_lines(
        self, lines: Sequence[PurchaseInvoiceLine], costs: Sequence[LandedCost]
    ) -> list[PostingLine]:
        """Cặp (khoản chi phí → dòng hàng) cho phần gốc + cặp thuế của từng khoản."""
        if not costs:
            return []
        result: list[PostingLine] = []
        pieces = pair_costs_with_lines(
            [cost.amount_fc for cost in costs], [line.landed_cost_fc for line in lines]
        )
        for piece in pieces:
            cost = costs[piece.cost_index]
            line = lines[piece.line_index]
            result.extend(
                self._pair(
                    debit_account=line.account_id,
                    debit_dimensions=_line_dimensions(line),
                    credit_account=cost.credit_account_id,
                    credit_dimensions=self._cost_dimensions(cost),
                    amount_fc=piece.amount_fc,
                    source_line_id=line.id,
                    description=f"{cost.description} — {line.description or self._voucher.voucher_no}",
                )
            )
        for cost in costs:
            if cost.vat_amount_fc > _ZERO and cost.vat_account_id is not None:
                result.extend(
                    self._pair(
                        debit_account=cost.vat_account_id,
                        debit_dimensions=_EMPTY_DIMENSIONS,
                        credit_account=cost.credit_account_id,
                        credit_dimensions=self._cost_dimensions(cost),
                        amount_fc=cost.vat_amount_fc,
                        source_line_id=cost.id,
                        description=cost.description,
                    )
                )
        return result

    def _cost_dimensions(self, cost: LandedCost) -> PostingDimensions:
        if cost.vendor_id is None:
            return _EMPTY_DIMENSIONS
        return PostingDimensions(partner_id=cost.vendor_id, partner_kind=PartnerKind.VENDOR)

    def _pair(
        self,
        *,
        debit_account: int,
        debit_dimensions: PostingDimensions,
        credit_account: int,
        credit_dimensions: PostingDimensions,
        amount_fc: Decimal,
        source_line_id: UUID,
        description: str | None,
    ) -> list[PostingLine]:
        """Một cặp Nợ/Có; chứng từ trả lại đảo chiều cả cặp."""
        return [
            self._line(
                account_id=debit_account,
                corresponding=credit_account,
                amount_fc=amount_fc,
                credit=self._reversed,
                dimensions=debit_dimensions,
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


def _line_dimensions(line: PurchaseInvoiceLine) -> PostingDimensions:
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
