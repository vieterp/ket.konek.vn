"""Số dư quỹ/tiền gửi tại một ngày (BR-QUY-01) — dùng chung cho guard và kiểm kê.

Tồn quỹ = dư đầu năm + phát sinh (Nợ − Có) tới hết ngày hạch toán, tính bằng
VND quy đổi trong phạm vi năm tài chính phủ ngày đó — cùng phép toán với đường
"tính thẳng" của bảng cân đối TK (`posting/balances/query_service.py`).

Gộp theo **số hiệu TK** chứ không `account_id`, cùng trục với trial balance
(quyết định 4F): cùng một "1111" ở hai gói cấu hình là hai `id`, và một dataset
đã đổi gói sẽ có phát sinh cũ treo trên `id` cũ — gộp theo id sẽ báo quỹ âm chỉ
vì tiền nằm ở id bên kia. So **đúng mã** (không gộp cây con): phát sinh chỉ đâm
vào TK hạch-toán-được, và câu hỏi của cả hai người gọi là về chính TK trên
chứng từ/biên bản.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.periods.service import fiscal_year_covering
from ket.posting.engine.models import GlPosting
from ket.posting.opening_balances.models import OpeningBalance

ZERO = Decimal(0)


def cash_balance_as_of(
    session: Session, *, ledger: int, branch_id: int, account_code: str, as_of: date
) -> Decimal:
    """Số dư VND (Nợ − Có) của một số hiệu TK trong một chi nhánh, hết ngày `as_of`.

    Chưa có năm tài chính phủ ngày này thì trả 0: người gọi ở đường ghi sổ sẽ
    đổ trước đó vì không tra được kỳ, còn người gọi ở đường kiểm kê nhận đúng
    "số sổ bằng 0" của một dataset chưa mở năm.
    """
    year = fiscal_year_covering(session, as_of)
    if year is None:
        return ZERO
    account_ids = select(ChartOfAccount.id).where(ChartOfAccount.code == account_code)
    opening = _opening_net(
        session, year_id=year.id, ledger=ledger, branch_id=branch_id, account_ids=account_ids
    )
    posted = (
        session.execute(
            select(func.coalesce(func.sum(GlPosting.debit - GlPosting.credit), 0)).where(
                GlPosting.ledger == ledger,
                GlPosting.branch_id == branch_id,
                GlPosting.account_id.in_(account_ids),
                GlPosting.posting_date >= year.start_date,
                GlPosting.posting_date <= as_of,
            )
        ).scalar_one()
        or ZERO
    )
    return opening + Decimal(posted)


def cash_balance_floor_from(
    session: Session, *, ledger: int, branch_id: int, account_code: str, from_date: date
) -> Decimal:
    """Số dư THẤP NHẤT của TK từ `from_date` tới hết năm tài chính.

    Guard chi-quá-tồn cần con số này chứ không phải số dư tại một ngày (review
    6B, M-2): một phiếu chi LÙI NGÀY trừ vào số dư của MỌI ngày từ ngày hạch
    toán trở đi — nó có thể không làm âm tại chính ngày đó nhưng làm âm một
    ngày sau, nơi quỹ đã bị các phiếu khác rút xuống. `min(số dư(t)) + delta`
    cho mọi `t ≥ from_date` chính là điều kiện "số dư 111x/112x < 0 sau khi
    ghi sổ" của FR-SYS-062 xét trên cả trục thời gian.

    Số ngày có phát sinh trong một năm ≤ 366 nên gộp theo ngày rồi gấp tích
    lũy ở Python — phần nặng (SUM theo ngày) vẫn là set-based SQL (LD-14).
    """
    year = fiscal_year_covering(session, from_date)
    if year is None:
        return ZERO
    account_ids = select(ChartOfAccount.id).where(ChartOfAccount.code == account_code)
    opening = _opening_net(
        session, year_id=year.id, ledger=ledger, branch_id=branch_id, account_ids=account_ids
    )
    before = (
        session.execute(
            select(func.coalesce(func.sum(GlPosting.debit - GlPosting.credit), 0)).where(
                GlPosting.ledger == ledger,
                GlPosting.branch_id == branch_id,
                GlPosting.account_id.in_(account_ids),
                GlPosting.posting_date >= year.start_date,
                GlPosting.posting_date < from_date,
            )
        ).scalar_one()
        or ZERO
    )
    daily = session.execute(
        select(GlPosting.posting_date, func.sum(GlPosting.debit - GlPosting.credit))
        .where(
            GlPosting.ledger == ledger,
            GlPosting.branch_id == branch_id,
            GlPosting.account_id.in_(account_ids),
            GlPosting.posting_date >= from_date,
            GlPosting.posting_date <= year.end_date,
        )
        .group_by(GlPosting.posting_date)
        .order_by(GlPosting.posting_date)
    ).all()

    running = opening + Decimal(before)
    # Số dư chỉ quan sát được ở CUỐI mỗi ngày: mốc khởi điểm (`running` trước
    # khi gấp) chỉ là một ứng viên khi chính `from_date` KHÔNG có phát sinh —
    # nếu có, số dư cuối ngày đó đã gồm phát sinh cùng ngày, và lấy mốc trước-
    # ngày làm floor sẽ báo âm cho một phiếu chi được chính phiếu thu cùng
    # ngày tài trợ.
    floor = running if not daily or daily[0][0] > from_date else Decimal("Infinity")
    for _posting_date, net in daily:
        running += Decimal(net)
        floor = min(floor, running)
    return floor


def _opening_net(
    session: Session,
    *,
    year_id: int,
    ledger: int,
    branch_id: int,
    account_ids: Select[tuple[int]],
) -> Decimal:
    return Decimal(
        session.execute(
            select(func.coalesce(func.sum(OpeningBalance.debit - OpeningBalance.credit), 0)).where(
                OpeningBalance.fiscal_year_id == year_id,
                OpeningBalance.ledger == ledger,
                OpeningBalance.branch_id == branch_id,
                OpeningBalance.account_id.in_(account_ids),
            )
        ).scalar_one()
        or ZERO
    )
