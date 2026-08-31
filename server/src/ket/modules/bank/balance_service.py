"""Số dư tiền gửi theo TỪNG tài khoản ngân hàng tại một ngày (lát 6E-1).

Song sinh với `cash_book/balance_service.py` cho phía ngân hàng, và là thứ màn
hình "Tiền vào tiền ra" (BFF `/api/v1/cashflow/overview`) đọc để dựng thẻ tài
khoản — món nợ lát 6D bàn giao: *số dư per-TK-ngân-hàng = số dư đầu kỳ nhóm
kind-1 + phát sinh, dùng lại khuôn `DepositMovementSource`*.

Vì sao không gọi thẳng `DepositMovementSource`: Protocol đó trả phát sinh của
**cả năm** (đủ cho carry-forward, người gọi duy nhất của nó) còn thẻ tài khoản
hỏi số dư **tới một ngày**. Hai đường khác nhau đúng ở mốc thời gian.

Từ lát 6G-1 cả hai chỉ còn ĐỌC cột `gl_postings.bank_account_id`: luật quy chủ
(BC/UNC/SEC theo thân, CTNB theo chiều) chuyển hẳn sang đường GHI trong
`bank/posting_mapper._deposit_owner`. Bốn bản chép của luật ấy — hai truy vấn
Python ở đây, `money_account_ledger.sql`, `bank_balance_summary.sql` — biến mất
cùng lúc, và phát sinh 112x của phiếu quỹ / bút toán tổng hợp lần đầu tiên có
mặt trong số dư từng tài khoản.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.bank.posting_mapper import MONEY_ACCOUNT_CODE_PREFIXES
from ket.posting.engine.models import GlPosting
from ket.posting.opening_balances.models import OpeningBalance

DEPOSIT_ACCOUNT_CODE_PREFIX = MONEY_ACCOUNT_CODE_PREFIXES[1]
"""Chỉ nhóm 112 — dòng 111x của một chứng từ chạm cả hai nhóm tiền (nộp/rút
tiền mặt) thuộc quỹ, không thuộc TK ngân hàng nào."""

ZERO = Decimal(0)


@dataclass(frozen=True)
class DepositBalance:
    """Số dư một tài khoản ngân hàng tại một ngày, cả hai trục tiền."""

    bank_account_id: int
    currency_code: str
    balance_fc: Decimal
    balance: Decimal


def deposit_balances_as_of(
    session: Session,
    *,
    ledger: int,
    branch_ids: Sequence[int],
    as_of: date,
) -> tuple[DepositBalance, ...]:
    """Số dư từng TK ngân hàng hết ngày `as_of`, cộng trên `branch_ids`.

    = số dư đầu năm gắn TK ngân hàng (`opening_balances` nhóm kind-1, cột
    `bank_account_id` có từ migration 0019) + **mọi** phát sinh 112x có chủ từ
    đầu năm tới hết `as_of`, bất kể chứng từ nào sinh ra nó.

    Trước lát 6G-1 câu này chỉ đúng với chứng từ tiền gửi, vì chủ sở hữu được
    suy từ thân `bank_vouchers`: phiếu quỹ nộp/rút tiền mặt ↔ ngân hàng (gói
    builtin khai sẵn `PT rut-tgnh-nhap-quy` và chiều ngược bằng phiếu chi) và
    bút toán tổng hợp gõ thẳng 112x không có thân để bám, nên rơi ra ngoài —
    nghiệp vụ hằng tuần chứ không phải ca hiếm (review 6E-1 H-3).

    Nay `bank_account` là chiều bắt buộc của 112x, nên phần "chưa gắn" chỉ còn
    là dữ liệu ghi sổ TRƯỚC lát này mà migration không suy nổi chủ. Con số đó
    vẫn hiện riêng trên màn hình thay vì bị giấu (ghi chú M-3 của review 6D).

    Chưa có năm tài chính phủ `as_of` thì trả rỗng — cùng hướng mặc định với
    `cash_balance_as_of`.
    """
    year = fiscal_year_covering(session, as_of)
    if year is None or not branch_ids:
        return ()

    branches = tuple(branch_ids)
    deposit_accounts = select(ChartOfAccount.id).where(
        ChartOfAccount.code.like(literal(f"{DEPOSIT_ACCOUNT_CODE_PREFIX}%"))
    )

    opening_rows = session.execute(
        select(
            OpeningBalance.bank_account_id,
            OpeningBalance.currency_code,
            func.sum(OpeningBalance.debit_fc - OpeningBalance.credit_fc).label("net_fc"),
            func.sum(OpeningBalance.debit - OpeningBalance.credit).label("net"),
        )
        .where(
            OpeningBalance.fiscal_year_id == year.id,
            OpeningBalance.ledger == ledger,
            OpeningBalance.branch_id.in_(branches),
            OpeningBalance.bank_account_id.is_not(None),
            # Lọc 112 y như `bank_balance_summary.sql` (review 6E-1 M-8): CHECK
            # của bảng ràng `bank_account_id IS NOT NULL` với NHÓM số dư, không
            # ràng với số hiệu TK — không có gì cấm một dòng nhóm-1 trỏ TK
            # ngoài 112. Thiếu mệnh đề này thì thẻ BFF và bảng kê số dư cho hai
            # con số cho cùng một tài khoản.
            OpeningBalance.account_id.in_(deposit_accounts),
        )
        .group_by(OpeningBalance.bank_account_id, OpeningBalance.currency_code)
    ).all()

    movement_rows = session.execute(
        select(
            GlPosting.bank_account_id,
            GlPosting.currency_code,
            func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
            func.sum(GlPosting.debit - GlPosting.credit).label("net"),
        )
        .where(
            GlPosting.ledger == ledger,
            GlPosting.branch_id.in_(branches),
            GlPosting.account_id.in_(deposit_accounts),
            GlPosting.posting_date >= year.start_date,
            GlPosting.posting_date <= as_of,
        )
        .group_by(GlPosting.bank_account_id, GlPosting.currency_code)
    ).all()

    totals: dict[tuple[int, str], list[Decimal]] = {}
    for row in (*opening_rows, *movement_rows):
        if row.bank_account_id is None:
            continue
        bucket = totals.setdefault((row.bank_account_id, row.currency_code), [ZERO, ZERO])
        bucket[0] += Decimal(row.net_fc or 0)
        bucket[1] += Decimal(row.net or 0)

    return tuple(
        DepositBalance(
            bank_account_id=bank_account_id,
            currency_code=currency_code,
            balance_fc=net_fc,
            balance=net,
        )
        for (bank_account_id, currency_code), (net_fc, net) in sorted(totals.items())
    )
