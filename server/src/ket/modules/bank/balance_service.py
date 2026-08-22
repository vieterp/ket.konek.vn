"""Số dư tiền gửi theo TỪNG tài khoản ngân hàng tại một ngày (lát 6E-1).

Song sinh với `cash_book/balance_service.py` cho phía ngân hàng, và là thứ màn
hình "Tiền vào tiền ra" (BFF `/api/v1/cashflow/overview`) đọc để dựng thẻ tài
khoản — món nợ lát 6D bàn giao: *số dư per-TK-ngân-hàng = số dư đầu kỳ nhóm
kind-1 + phát sinh, dùng lại khuôn `DepositMovementSource`*.

Vì sao không gọi thẳng `DepositMovementSource`: Protocol đó trả phát sinh của
**cả năm** (đủ cho carry-forward, người gọi duy nhất của nó) còn thẻ tài khoản
hỏi số dư **tới một ngày**. Thay vì nới hợp đồng kernel sắp đóng băng ở 6G cho
một người gọi mới, hai đường dùng CHUNG đúng phần dễ sai — biểu thức quy chủ TK
ngân hàng (`deposit_owner_account`) — và khác nhau ở phần tầm thường là mốc
thời gian.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, case, func, literal, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.bank.models import BankVoucher, BankVoucherKind
from ket.modules.bank.posting_mapper import MONEY_ACCOUNT_CODE_PREFIXES
from ket.posting.engine.models import GlPosting
from ket.posting.opening_balances.models import OpeningBalance

DEPOSIT_ACCOUNT_CODE_PREFIX = MONEY_ACCOUNT_CODE_PREFIXES[1]
"""Chỉ nhóm 112 — dòng 111x trên chứng từ tiền gửi (nộp/rút tiền mặt) thuộc
quỹ, không thuộc TK ngân hàng nào."""

ZERO = Decimal(0)


def deposit_owner_account() -> ColumnElement[int | None]:
    """TK ngân hàng SỞ HỮU một dòng sổ 112x của chứng từ tiền gửi.

    Luật (SRS 04 §2, lát 6C/6D) sống ở ĐÚNG MỘT chỗ trong Python vì nó là thứ
    dễ sai và có nhiều người dùng: BC/UNC/SEC thì mọi dòng 112x thuộc
    `bank_account_id` của thân chứng từ; CTNB thì dòng **Nợ** thuộc TK ĐÍCH
    (`counter_bank_account_id`) và dòng **Có** thuộc TK nguồn — một chứng từ
    đứng trên sổ của cả hai tài khoản, mỗi bên một chiều.

    Người gọi: `movement_source.BankDepositMovementSource` (carry-forward) và
    `deposit_balances_as_of` (thẻ tài khoản). Hai dataset báo cáo
    (`money_account_ledger.sql`, `bank_balance_summary.sql`) lặp lại chính luật
    này bằng SQL thuần — bản sao có chủ ý và ĐÃ GHI vào sổ tổng quát hóa của
    bước 22: hợp nhất chúng cần một view/hàm SQL dùng chung, việc của lát kiểm
    định 6G chứ không phải của lát này.
    """
    return case(
        (
            (BankVoucher.kind == BankVoucherKind.INTERNAL_TRANSFER) & (GlPosting.debit > 0),
            BankVoucher.counter_bank_account_id,
        ),
        else_=BankVoucher.bank_account_id,
    )


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
    `bank_account_id` có từ migration 0019) + phát sinh của chứng từ tiền gửi
    từ đầu năm tới hết `as_of`.

    Phát sinh 112x KHÔNG quy được về một tài khoản ngân hàng nào thì KHÔNG có
    mặt ở đây. Hai nguồn, không phải một (review 6E-1 H-3): bút toán GLE gõ
    thẳng vào 112x, **và chứng từ QUỸ chạm 112** — gói builtin khai sẵn
    `PT rut-tgnh-nhap-quy` cùng chiều ngược bằng phiếu chi, chúng là
    `cash_vouchers` nên không có `bank_vouchers` để bám. Nộp/rút tiền mặt ↔
    ngân hàng là nghiệp vụ hằng tuần, nên đây không phải ca hiếm.

    Phần đó vẫn nằm trong tổng TK 112 của bảng cân đối; chênh giữa "tổng thẻ
    ngân hàng" và "tổng TK 112" chính là phần chưa gắn, và màn hình hiển thị nó
    thành một con số riêng thay vì giấu (ghi chú M-3 của review 6D). Nợ "thêm
    `bank_account_id` cho chứng từ quỹ" ghi bàn giao 6E-1 → 6G.

    Chưa có năm tài chính phủ `as_of` thì trả rỗng — cùng hướng mặc định với
    `cash_balance_as_of`.
    """
    year = fiscal_year_covering(session, as_of)
    if year is None or not branch_ids:
        return ()

    branches = tuple(branch_ids)
    owner = deposit_owner_account().label("bank_account_id")
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
            owner,
            GlPosting.currency_code,
            func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
            func.sum(GlPosting.debit - GlPosting.credit).label("net"),
        )
        .join(BankVoucher, BankVoucher.id == GlPosting.voucher_id)
        .where(
            GlPosting.ledger == ledger,
            GlPosting.branch_id.in_(branches),
            GlPosting.account_id.in_(deposit_accounts),
            GlPosting.posting_date >= year.start_date,
            GlPosting.posting_date <= as_of,
        )
        .group_by(owner, GlPosting.currency_code)
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
