"""Dịch dòng chi tiết → `PostingRequest` (phase-04 §Chứng từ).

Đây là hợp đồng một chiều giữa module và engine: engine không đọc
`gl_journal_lines`, module không chạm `gl_postings`. Chứng từ nghiệp vụ khác
ghi hai sổ **giống nhau** nên `management_lines` để `None` — engine tự nhân
bản (LD-07; chứng từ ghi khác nhau giữa hai sổ xuất hiện từ phase 9).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ket.modules.general_ledger.journal.models import JournalLine
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PartnerKind,
    PostingDimensions,
    PostingLine,
    PostingRequest,
)


def to_posting_request(voucher_id: UUID, lines: Sequence[JournalLine]) -> PostingRequest:
    return PostingRequest(
        voucher_id=voucher_id,
        financial_lines=tuple(_to_posting_line(line) for line in lines),
        management_lines=None,
    )


def _to_posting_line(line: JournalLine) -> PostingLine:
    extended = tuple(
        ExtendedDimensionValue(dimension_id=int(dimension_id), value_id=value_id)
        for dimension_id, value_id in sorted((line.extended_dimensions or {}).items())
    )
    return PostingLine(
        account_id=line.account_id,
        corresponding_account_id=line.corresponding_account_id,
        debit_fc=line.debit_fc,
        credit_fc=line.credit_fc,
        currency=line.currency_code,
        rate=line.exchange_rate,
        dimensions=PostingDimensions(
            partner_id=line.partner_id,
            partner_kind=(
                PartnerKind(line.partner_kind) if line.partner_kind is not None else None
            ),
            cost_object_id=line.cost_object_id,
            project_id=line.project_id,
            order_id=line.order_id,
            contract_id=line.contract_id,
            expense_item_id=line.expense_item_id,
            item_id=line.item_id,
            warehouse_id=line.warehouse_id,
            bank_account_id=line.bank_account_id,
            extended=extended,
        ),
        source_line_id=line.id,
        description=line.description,
    )
