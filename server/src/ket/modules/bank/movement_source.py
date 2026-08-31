"""Bản cài `DepositMovementSource` — phát sinh tiền gửi ròng theo TK ngân hàng.

Người gọi duy nhất là job carry-forward của số dư đầu kỳ (`posting` không
import được module này — luật phụ thuộc C4 — nên đi qua Protocol kernel, lát
6D). Câu hỏi nó trả lời: trong một năm, mỗi TK ngân hàng đã nhận/chi bao nhiêu.

Cách tính từ lát 6G-1: mọi dòng sổ 112x (`DEPOSIT_ACCOUNT_CODE_PREFIX` —
doctrine mapper 6C) đã mang sẵn chủ sở hữu ở cột `gl_postings.bank_account_id`,
gộp theo cột đó. Trước đó chủ sở hữu phải SUY từ thân `bank_vouchers`, nên câu
trả lời chỉ phủ chứng từ tiền gửi: phiếu quỹ nộp/rút tiền mặt ↔ ngân hàng và
bút toán tổng hợp gõ thẳng 112x không có thân để bám và biến mất khỏi số dư
chuyển sang năm sau (review 6E-1 H-3). Luật quy chủ nay sống ở đường GHI —
`bank/posting_mapper._deposit_owner`.

Gộp set-based trong SQL (LD-14); kết quả cỡ (số TK ngân hàng × tiền tệ) — vài
chục dòng.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.protocols import PROVIDERS, BankAccountMovement
from ket.modules.bank.balance_service import DEPOSIT_ACCOUNT_CODE_PREFIX
from ket.posting.engine.models import GlPosting


class BankDepositMovementSource:
    """Xem docstring module."""

    def deposit_movements(
        self, session: Session, *, fiscal_year_id: int, ledger: int, branch_id: int
    ) -> Sequence[BankAccountMovement]:
        period_ids = select(AccountingPeriod.id).where(
            AccountingPeriod.fiscal_year_id == fiscal_year_id
        )
        rows = session.execute(
            select(
                GlPosting.account_id,
                GlPosting.bank_account_id,
                GlPosting.currency_code,
                func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
                func.sum(GlPosting.debit - GlPosting.credit).label("net"),
            )
            .join(ChartOfAccount, ChartOfAccount.id == GlPosting.account_id)
            .where(
                GlPosting.ledger == ledger,
                GlPosting.branch_id == branch_id,
                GlPosting.period_id.in_(period_ids),
                ChartOfAccount.code.like(literal(f"{DEPOSIT_ACCOUNT_CODE_PREFIX}%")),
            )
            .group_by(GlPosting.account_id, GlPosting.bank_account_id, GlPosting.currency_code)
        ).all()
        return tuple(
            BankAccountMovement(
                account_id=row.account_id,
                bank_account_id=row.bank_account_id,
                currency_code=row.currency_code,
                net_fc=row.net_fc,
                net=row.net,
            )
            for row in rows
            # Dòng 112x không có chủ (dữ liệu ghi sổ trước lát 6G-1, hoặc gói
            # cấu hình chưa bật chiều `bank_account`) thì bỏ — nó ở lại phần
            # không gắn của carry-forward, không làm đổ cả lượt chuyển.
            if row.bank_account_id is not None and (row.net != 0 or row.net_fc != 0)
        )


def register() -> None:
    PROVIDERS.register_deposit_movement_source(BankDepositMovementSource())
