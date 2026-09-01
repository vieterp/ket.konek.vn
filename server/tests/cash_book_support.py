"""Khung dựng bối cảnh cho test phân hệ Quỹ (lát 6B).

Bồi thêm lên `posting_support.seed_posting_context`: gói `TT99-TEST` thắng
`resolve_package` trong dataset test (xem docstring bên đó), nên nghiệp vụ
định khoản (FR-SYS-025) và TK ngầm định mà phiếu thu/chi cần phải được gieo
vào CHÍNH gói đó — cùng lối `TestAutoPostingOperations` của lát 6A, nhưng
idempotent để nhiều tệp test dùng chung dataset không giẫm nhau.

Cố ý KHÔNG gieo purpose `cash`/`ar_trade` (tệp test khác của 6A tự gieo rồi
xóa cặp `(*, cash)` trong teardown của nó — đụng khóa chính); các purpose ở
đây (`fx_gain`, `fx_loss`, `unexplained_*`) là của riêng nhóm test quỹ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DefaultAccount
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

_EXTRA_ACCOUNT_SPECS: tuple[tuple[str, str, int], ...] = (
    # (code, name, balance_nature) — TK mà nghiệp vụ quỹ trỏ tới, thêm vào gói test.
    ("515", "Doanh thu hoạt động tài chính", BalanceNature.NONE),
    ("635", "Chi phí tài chính", BalanceNature.NONE),
    ("1381", "Tài sản thiếu chờ xử lý", BalanceNature.DEBIT),
    ("3381", "Tài sản thừa chờ giải quyết", BalanceNature.CREDIT),
)

_DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("fx_gain", "515"),
    ("fx_loss", "635"),
    ("unexplained_surplus", "3381"),
    ("unexplained_shortage", "1381"),
)

_RULES: tuple[tuple[str, str, str, str | None, str | None, bool, int | None, int], ...] = (
    # (document_type, code, name, debit_purpose, credit_purpose, requires_partner, partner_kind, order)
    ("PT", "thu-khac", "Thu khác", None, None, False, None, 1),
    ("PT", "thu-no-khach-hang", "Khách hàng trả nợ", None, None, True, 0, 2),
    ("PT", "thua-quy-kiem-ke", "Thừa quỹ kiểm kê", None, "unexplained_surplus", False, None, 3),
    ("PC", "chi-khac", "Chi khác", None, None, False, None, 1),
    ("PC", "tra-no-ncc", "Trả nợ nhà cung cấp", None, None, True, 1, 2),
    ("PC", "thieu-quy-kiem-ke", "Thiếu quỹ kiểm kê", "unexplained_shortage", None, False, None, 3),
)


def seed_cash_book_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo TK + purpose + nghiệp vụ quỹ vào gói test; trả bảng số hiệu → id
    (gộp cả bộ TK sẵn có của `context`). Idempotent theo khóa tự nhiên."""
    accounts = dict(context.accounts)
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        for code, name, nature in _EXTRA_ACCOUNT_SPECS:
            row = session.scalar(
                select(ChartOfAccount).where(
                    ChartOfAccount.package_id == context.package_id, ChartOfAccount.code == code
                )
            )
            if row is None:
                row = ChartOfAccount(
                    package_id=context.package_id,
                    code=code,
                    name=name,
                    path="0.",
                    balance_nature=nature,
                    is_summary=False,
                )
                session.add(row)
                session.flush()
                row.path = f"{row.id}."
                session.flush()
            accounts[code] = row.id

        for purpose, account_code in _DEFAULT_ACCOUNTS:
            if session.get(DefaultAccount, (context.package_id, "*", purpose)) is None:
                session.add(
                    DefaultAccount(
                        package_id=context.package_id,
                        document_type="*",
                        purpose=purpose,
                        account_code=account_code,
                    )
                )
        for document_type, code, name, debit, credit, requires, partner_kind, order in _RULES:
            existing = session.scalar(
                select(AutoPostingRule.id).where(
                    AutoPostingRule.package_id == context.package_id,
                    AutoPostingRule.document_type == document_type,
                    AutoPostingRule.operation_code == code,
                )
            )
            if existing is None:
                session.add(
                    AutoPostingRule(
                        package_id=context.package_id,
                        document_type=document_type,
                        operation_code=code,
                        operation_name=name,
                        debit_purpose=debit,
                        credit_purpose=credit,
                        requires_partner=requires,
                        partner_kind=partner_kind,
                        display_order=order,
                    )
                )
        session.flush()
    return accounts


def seed_open_invoice(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    *,
    detail_kind: int = OpeningDetailKind.RECEIVABLE,
    account_code: str = "131",
    partner_kind: PartnerKind = PartnerKind.CUSTOMER,
    partner_id: int = 71,
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    amount_fc: Decimal = Decimal("1000000"),
    amount: Decimal | None = None,
    invoice_no: str = "HD-0001",
    invoice_date: date = date(2025, 12, 20),
    due_date: date | None = date(2026, 2, 20),
    ledger: int = 0,
) -> UUID:
    """Một dòng số dư công nợ + một chứng từ con còn nợ; trả `id` chứng từ con."""
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    resolved_amount = amount if amount is not None else amount_fc * exchange_rate
    with unit_of_work(session_factory, scope) as session:
        side_debit = detail_kind == OpeningDetailKind.RECEIVABLE
        parent = OpeningBalance(
            fiscal_year_id=context.fiscal_year_id,
            ledger=ledger,
            branch_id=context.branch_id,
            account_id=context.accounts[account_code],
            currency_code=currency_code,
            exchange_rate=exchange_rate,
            partner_id=partner_id,
            partner_kind=partner_kind.value,
            detail_kind=detail_kind,
            debit_fc=amount_fc if side_debit else Decimal(0),
            credit_fc=Decimal(0) if side_debit else amount_fc,
            debit=resolved_amount if side_debit else Decimal(0),
            credit=Decimal(0) if side_debit else resolved_amount,
        )
        session.add(parent)
        session.flush()
        invoice = OpeningBalanceInvoice(
            opening_balance_id=parent.id,
            branch_id=parent.branch_id,
            invoice_no=invoice_no,
            invoice_date=invoice_date,
            due_date=due_date,
            amount_fc=amount_fc,
            amount=resolved_amount,
        )
        session.add(invoice)
        session.flush()
        return invoice.id
