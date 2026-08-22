"""Bản cài `DepositMovementSource` — phát sinh tiền gửi ròng theo TK ngân hàng.

Người gọi duy nhất là job carry-forward của số dư đầu kỳ (`posting` không
import được module này — luật phụ thuộc C4 — nên đi qua Protocol kernel, lát
6D). Câu hỏi nó trả lời: trong một năm, mỗi TK ngân hàng đã nhận/chi bao nhiêu
qua **chứng từ tiền gửi đã ghi sổ** — thứ `gl_postings` một mình không nói
được, vì chiều TK ngân hàng sống trên thân `bank_vouchers` (FR-BNK-002).

Cách tính: dòng sổ (`gl_postings`) của chứng từ tiền gửi, lọc bên tiền gửi
theo tiền tố 112 (`DEPOSIT_ACCOUNT_CODE_PREFIX` — doctrine mapper 6C), quy về
TK ngân hàng bằng `balance_service.deposit_owner_account()` — luật quy chủ ở
ĐÚNG một chỗ, dùng chung với thẻ tài khoản của màn hình "Tiền vào tiền ra":

* BC/UNC/SEC — mọi dòng 112x thuộc `bank_account_id` của thân chứng từ (một
  chứng từ một tài khoản; dòng phí lẫn trong chứng từ tự trừ vào net, đúng số
  tiền thật qua tài khoản);
* CTNB — dòng **Nợ** 112x thuộc TK đích (`counter_bank_account_id`), dòng
  **Có** 112x thuộc TK nguồn (`bank_account_id`) — một chứng từ chạm hai tài
  khoản, mỗi bên một chiều (6C: chuyển nội bộ cùng tiền tệ).

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
from ket.modules.bank.balance_service import (
    DEPOSIT_ACCOUNT_CODE_PREFIX,
    deposit_owner_account,
)
from ket.modules.bank.models import BankVoucher
from ket.posting.engine.models import GlPosting


class BankDepositMovementSource:
    """Xem docstring module."""

    def deposit_movements(
        self, session: Session, *, fiscal_year_id: int, ledger: int, branch_id: int
    ) -> Sequence[BankAccountMovement]:
        period_ids = select(AccountingPeriod.id).where(
            AccountingPeriod.fiscal_year_id == fiscal_year_id
        )
        owner_account = deposit_owner_account().label("bank_account_id")
        rows = session.execute(
            select(
                GlPosting.account_id,
                owner_account,
                GlPosting.currency_code,
                func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
                func.sum(GlPosting.debit - GlPosting.credit).label("net"),
            )
            .join(BankVoucher, BankVoucher.id == GlPosting.voucher_id)
            .join(ChartOfAccount, ChartOfAccount.id == GlPosting.account_id)
            .where(
                GlPosting.ledger == ledger,
                GlPosting.branch_id == branch_id,
                GlPosting.period_id.in_(period_ids),
                ChartOfAccount.code.like(literal(f"{DEPOSIT_ACCOUNT_CODE_PREFIX}%")),
            )
            .group_by(GlPosting.account_id, owner_account, GlPosting.currency_code)
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
            # CTNB đích NULL không xảy ra (CHECK `counter_account_iff_transfer`)
            # nhưng phòng thủ rẻ: một dòng không quy được chủ thì bỏ — nó ở lại
            # phần không gắn của carry-forward, không làm đổ cả lượt chuyển.
            if row.bank_account_id is not None and (row.net != 0 or row.net_fc != 0)
        )


def register() -> None:
    PROVIDERS.register_deposit_movement_source(BankDepositMovementSource())
