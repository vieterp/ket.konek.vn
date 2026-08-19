"""Danh sách phát sinh sổ cái — đường đọc thẳng `gl_postings` (lát 4E).

Khác bảng cân đối (snapshot + dấu bẩn), danh sách phát sinh KHÔNG có bản
snapshot: `gl_postings` chỉ tồn tại khi chứng từ đã ghi sổ (N1) nên đọc thẳng
luôn đúng — không cần cờ `stale`. Đây cũng là nền cho drill-down sổ cái ở
phase 10a (U10): mở một dòng bảng cân đối là lọc danh sách này theo TK + kỳ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.posting.documents.models import Voucher
from ket.posting.engine.models import GlPosting


@dataclass(frozen=True)
class PostingRow:
    """Một dòng phát sinh kèm ngữ cảnh chứng từ + số hiệu TK để đọc được ngay."""

    id: int
    voucher_id: UUID
    voucher_no: str
    document_type: str
    line_no: int
    ledger: int
    branch_id: int
    posting_date: date
    period_id: int
    account_id: int
    account_code: str
    account_name: str
    corresponding_account_id: int | None
    currency_code: str
    exchange_rate: Decimal
    debit_fc: Decimal
    credit_fc: Decimal
    debit: Decimal
    credit: Decimal
    partner_id: int | None
    partner_kind: int | None
    description: str | None


@dataclass(frozen=True)
class PostingsPage:
    """Một trang phát sinh + tổng số dòng khớp bộ lọc."""

    rows: tuple[PostingRow, ...]
    total: int


def postings_page(
    session: Session,
    *,
    ledger: int,
    account_id: int | None = None,
    period_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    branch_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> PostingsPage:
    """Trang phát sinh theo bộ lọc, cũ trước — thứ tự đọc sổ.

    `branch_id` chỉ thu hẹp trong phạm vi RLS đã lọc (cùng hợp đồng
    `trial_balance`). Lọc ngày theo `posting_date`; lọc theo kỳ dùng
    `period_id` — kỳ 13 (10a) sẽ phá tương đương ngày ↔ kỳ nên hai bộ lọc
    tồn tại song song thay vì suy cái này từ cái kia.
    """
    conditions = [GlPosting.ledger == ledger]
    if account_id is not None:
        conditions.append(GlPosting.account_id == account_id)
    if period_id is not None:
        conditions.append(GlPosting.period_id == period_id)
    if from_date is not None:
        conditions.append(GlPosting.posting_date >= from_date)
    if to_date is not None:
        conditions.append(GlPosting.posting_date <= to_date)
    if branch_id is not None:
        conditions.append(GlPosting.branch_id == branch_id)

    total = session.scalar(select(func.count()).select_from(GlPosting).where(*conditions)) or 0
    rows = session.execute(
        select(GlPosting, Voucher.voucher_no, Voucher.document_type, ChartOfAccount)
        .join(Voucher, Voucher.id == GlPosting.voucher_id)
        .join(ChartOfAccount, ChartOfAccount.id == GlPosting.account_id)
        .where(*conditions)
        # `id` chốt đuôi làm tiebreaker duy nhất: hai chứng từ khác loại/chi
        # nhánh được phép trùng (ngày, số) — thiếu nó thì thứ tự giữa hai trang
        # không xác định và dòng có thể lặp/thiếu khi lật trang.
        .order_by(GlPosting.posting_date, Voucher.voucher_no, GlPosting.line_no, GlPosting.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PostingsPage(
        rows=tuple(
            PostingRow(
                id=posting.id,
                voucher_id=posting.voucher_id,
                voucher_no=voucher_no,
                document_type=document_type,
                line_no=posting.line_no,
                ledger=posting.ledger,
                branch_id=posting.branch_id,
                posting_date=posting.posting_date,
                period_id=posting.period_id,
                account_id=posting.account_id,
                account_code=account.code,
                account_name=account.name,
                corresponding_account_id=posting.corresponding_account_id,
                currency_code=posting.currency_code,
                exchange_rate=posting.exchange_rate,
                debit_fc=posting.debit_fc,
                credit_fc=posting.credit_fc,
                debit=posting.debit,
                credit=posting.credit,
                partner_id=posting.partner_id,
                partner_kind=posting.partner_kind,
                description=posting.description,
            )
            for posting, voucher_no, document_type, account in rows
        ),
        total=total,
    )
