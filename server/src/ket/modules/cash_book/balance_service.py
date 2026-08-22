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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.periods.service import fiscal_year_covering
from ket.posting.engine.models import GlPosting
from ket.posting.opening_balances.models import OpeningBalance

CASH_ACCOUNT_CODE_PREFIXES = ("111", "112")
"""Nhóm TK tiền mặt/tiền gửi — soi theo TIỀN TỐ số hiệu, không theo danh sách id.

Đây là literal số hiệu TK **có chủ đích**, đúng loại mà doctrine "không
hard-code hệ TK" (SRS 19 §9 #1) cho phép đặt tên ở một hằng: điều kiện kích
hoạt của cảnh báo chi-quá-tồn do CHÍNH SRS định nghĩa bằng số hiệu ("Số dư TK
111x/112x < 0" — `docs/srs/01` §8.2), và nhóm 111/112 = tiền là bất biến chung
của cả TT99 lẫn TT133 — không phải một đích hạch toán cấu hình được như 641/642.
Nếu một chế độ kế toán tương lai đổi nhóm TK tiền, chỗ sửa là MỘT hằng này.

Ở `balance_service` chứ không ở `guards` (nơi nó ra đời ở lát 6B): guards
import balance_service, nên hằng phải nằm ở đáy đồ thị phụ thuộc để cả hai —
và `cash_balances_as_of` của lát 6E-1 — dùng chung mà không tạo vòng import."""

CASH_ON_HAND_PREFIX = CASH_ACCOUNT_CODE_PREFIXES[0]
"""Riêng tiền mặt tại quỹ (111x) — kiểm kê quỹ chỉ áp cho két tiền, không áp
cho tài khoản ngân hàng."""

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


@dataclass(frozen=True)
class CashAccountBalance:
    """Số dư một số hiệu TK quỹ, cộng trên nhiều chi nhánh."""

    account_id: int
    account_code: str
    account_name: str
    balance: Decimal


def cash_balances_as_of(
    session: Session,
    *,
    ledger: int,
    branch_ids: Sequence[int],
    as_of: date,
) -> tuple[CashAccountBalance, ...]:
    """Số dư MỌI tài khoản quỹ tiền mặt hết ngày `as_of`, cộng trên `branch_ids`.

    Thẻ tài khoản của màn hình "Tiền vào tiền ra" (BFF `/cashflow/overview`)
    đọc hàm này. Khác `cash_balance_as_of` ở hai chỗ, và cả hai đều là nhu cầu
    của màn hình chứ không phải của guard: cộng NHIỀU chi nhánh trong phạm vi
    người dùng (guard xét đúng chi nhánh của phiếu), và trả MỌI tài khoản một
    lượt thay vì hỏi từng mã (một truy vấn thay vì N).

    Cùng phép toán, cùng trục gộp **theo số hiệu TK** với `cash_balance_as_of`
    (quyết định 4F) — hai đường phải cho cùng con số cho cùng một tài khoản
    trong cùng một chi nhánh, và test ghim đúng điều đó.

    `account_id` trả về là id NHỎ NHẤT của số hiệu đó: nó chỉ dùng để client
    gọi tiếp `/cashflow/transactions`, còn danh tính hiển thị là số hiệu.
    """
    year = fiscal_year_covering(session, as_of)
    if year is None or not branch_ids:
        return ()
    branches = tuple(branch_ids)
    cash_accounts = select(ChartOfAccount.id).where(
        ChartOfAccount.code.like(f"{CASH_ON_HAND_PREFIX}%")
    )

    opening = (
        select(
            OpeningBalance.account_id.label("account_id"),
            (func.sum(OpeningBalance.debit - OpeningBalance.credit)).label("net"),
        )
        .where(
            OpeningBalance.fiscal_year_id == year.id,
            OpeningBalance.ledger == ledger,
            OpeningBalance.branch_id.in_(branches),
            OpeningBalance.account_id.in_(cash_accounts),
        )
        .group_by(OpeningBalance.account_id)
    )
    movement = (
        select(
            GlPosting.account_id.label("account_id"),
            (func.sum(GlPosting.debit - GlPosting.credit)).label("net"),
        )
        .where(
            GlPosting.ledger == ledger,
            GlPosting.branch_id.in_(branches),
            GlPosting.account_id.in_(cash_accounts),
            GlPosting.posting_date >= year.start_date,
            GlPosting.posting_date <= as_of,
        )
        .group_by(GlPosting.account_id)
    )
    combined = opening.union_all(movement).subquery()

    rows = session.execute(
        select(
            func.min(ChartOfAccount.id).label("account_id"),
            ChartOfAccount.code,
            func.min(ChartOfAccount.name).label("name"),
            func.coalesce(func.sum(combined.c.net), 0).label("balance"),
        )
        .join(combined, combined.c.account_id == ChartOfAccount.id)
        .group_by(ChartOfAccount.code)
        .order_by(ChartOfAccount.code)
    ).all()
    return tuple(
        CashAccountBalance(
            account_id=row.account_id,
            account_code=row.code,
            account_name=row.name,
            balance=Decimal(row.balance),
        )
        for row in rows
    )


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
