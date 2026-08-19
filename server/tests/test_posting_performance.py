"""Ghi sổ một chứng từ 10 dòng phải xong dưới 2 giây (FR-NFR-040, bước 20).

Cùng quy ước với `test_recalc_performance`: mốc là hằng số module, số đo in ra
để dán vào báo cáo phase, và test khẳng định việc ĐÃ xảy ra (đủ dòng, đúng
trạng thái) — một vòng lặp rỗng không qua được đây.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
POST_BUDGET_SECONDS: Final[float] = 2.0
LINE_COUNT: Final[int] = 10
APR_9 = date(2026, 4, 9)


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


def _ten_line_payload(context: PostingContext) -> JournalVoucherIn:
    """5 dòng nợ 642 + 5 dòng có 111, mỗi bên 5 số tiền khác nhau — cân nhau."""
    amounts = [Decimal(amount) for amount in (100_000, 200_000, 300_000, 400_000, 500_000)]
    debit_lines = tuple(
        JournalLineIn(account_id=context.accounts["642"], debit_fc=amount) for amount in amounts
    )
    credit_lines = tuple(
        JournalLineIn(account_id=context.accounts["111"], credit_fc=amount) for amount in amounts
    )
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=APR_9,
        posting_date=APR_9,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="đo FR-NFR-040: ghi sổ chứng từ 10 dòng",
        lines=debit_lines + credit_lines,
    )


def test_posting_a_ten_line_voucher_stays_under_budget(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        voucher_id = (
            JournalVoucherService(session).create(_ten_line_payload(context), user_id=ACTOR_ID).id
        )

    # Đo trọn transaction ghi sổ — FOR SHARE hàng kỳ, validator, INSERT hai sổ,
    # đánh dấu recalc, commit — vì "dưới 2 giây" của FR-NFR-040 là thứ người
    # dùng chờ sau cú bấm, không phải riêng câu INSERT.
    started = time.monotonic()
    with unit_of_work(session_factory, scope) as session:
        JournalVoucherService(session).post(voucher_id, user_id=ACTOR_ID)
    elapsed = time.monotonic() - started
    print(f"post 10 dòng: {elapsed:.3f}s (mốc < {POST_BUDGET_SECONDS}s)")  # noqa: T201 — số đo cho báo cáo phase

    with unit_of_work(session_factory, scope) as session:
        status = session.execute(
            select(Voucher.status).where(Voucher.id == voucher_id)
        ).scalar_one()
        assert status == VoucherStatus.DA_GHI_SO.value
        posted_lines = session.execute(
            select(func.count()).select_from(GlPosting).where(GlPosting.voucher_id == voucher_id)
        ).scalar_one()
        assert posted_lines == LINE_COUNT * 2  # 10 dòng × 2 sổ

    assert elapsed < POST_BUDGET_SECONDS, (
        f"ghi sổ chứng từ 10 dòng mất {elapsed:.3f}s (mốc < {POST_BUDGET_SECONDS}s)"
    )
