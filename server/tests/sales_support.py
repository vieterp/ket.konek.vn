"""Khung dựng bối cảnh cho test phân hệ Bán hàng (lát 7C-2).

Bồi lên `posting_support.seed_posting_context` cùng lối `purchase_support`:
gói `TT99-TEST` thắng `resolve_package` trong dataset test, nên TK doanh thu /
thuế đầu ra / giảm trừ doanh thu và nghiệp vụ `SAL` (FR-SYS-025) phải được gieo
vào CHÍNH gói đó — idempotent theo khóa tự nhiên để nhiều tệp test dùng chung
dataset không giẫm nhau (kể cả giẫm lên phần `cash_book_support` và
`purchase_support` gieo: cùng 515/635 và cùng cặp `(*, fx_gain/fx_loss)`).

`131` ở đây theo dõi **khách hàng** — chính phép kiểm mà
`SalesInvoiceService._verify_customer_tracked_account` canh; `1311` là TK phải
thu **thứ hai** để kiểm ca "đối trừ vào khoản nợ nằm trên TK khác", đối xứng
với `3388` của chiều mua.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DefaultAccount
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

_EXTRA_ACCOUNT_SPECS: tuple[tuple[str, str, int, list[str] | None], ...] = (
    # (code, name, balance_nature, detail_tracking)
    ("131", "Phải thu của khách hàng", BalanceNature.DUAL, ["customer"]),
    # TK phải thu THỨ HAI theo dõi khách hàng — để kiểm "đối trừ vào khoản nợ
    # nằm trên TK khác" mà không phải bịa một TK không theo dõi đối tác.
    ("1311", "Phải thu khách hàng — nhóm hai", BalanceNature.DUAL, ["customer"]),
    ("511", "Doanh thu bán hàng và cung cấp dịch vụ", BalanceNature.CREDIT, None),
    ("5111", "Doanh thu bán hàng hóa", BalanceNature.CREDIT, None),
    ("5112", "Doanh thu cung cấp dịch vụ", BalanceNature.CREDIT, None),
    ("521", "Các khoản giảm trừ doanh thu", BalanceNature.CREDIT, None),
    ("33311", "Thuế GTGT đầu ra phải nộp", BalanceNature.CREDIT, None),
    ("515", "Doanh thu hoạt động tài chính", BalanceNature.NONE, None),
    ("635", "Chi phí tài chính", BalanceNature.NONE, None),
)

_DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (("fx_gain", "515"), ("fx_loss", "635"))

_RULES: tuple[tuple[str, str, str, bool, int | None, int], ...] = (
    # (document_type, code, name, requires_partner, partner_kind, order)
    ("SAL", "ban-hang-hoa", "Bán hàng hóa trong nước", True, 0, 1),
    ("SAL", "ban-dich-vu", "Bán dịch vụ", True, 0, 2),
    ("SAL", "ban-hang-dai-ly", "Bán hàng qua đại lý", True, 0, 3),
    ("SAL", "tra-lai-hang-ban", "Hàng bán bị trả lại", True, 0, 4),
    ("SAL", "giam-gia-hang-ban", "Giảm giá hàng bán", True, 0, 5),
    # Nghiệp vụ gói khai sai loại đối tác — để kiểm module từ chối.
    ("SAL", "ban-cho-ncc", "Nghiệp vụ khai nhầm cho nhà cung cấp", True, 1, 9),
)


def seed_sales_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo TK + purpose + nghiệp vụ bán vào gói test; trả bảng số hiệu → id."""
    accounts = dict(context.accounts)
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        for code, name, nature, tracking in _EXTRA_ACCOUNT_SPECS:
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
                    detail_tracking=tracking,
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
        for document_type, code, name, requires, partner_kind, order in _RULES:
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
                        requires_partner=requires,
                        partner_kind=partner_kind,
                        display_order=order,
                    )
                )
        session.flush()
    return accounts


def ensure_customer(
    session: Session,
    *,
    partner_id: int,
    code: str,
    credit_limit: Decimal | None = None,
    payment_term_id: int | None = None,
) -> int:
    """Một khách hàng với `id` cố định — idempotent để fixture module dùng lại."""
    existing = session.get(Partner, partner_id)
    if existing is None:
        session.add(
            Partner(
                id=partner_id,
                code=code,
                name=f"Khách hàng {code}",
                path=f"{partner_id}.",
                is_customer=True,
                credit_limit=credit_limit,
                payment_term_id=payment_term_id,
            )
        )
    else:
        existing.credit_limit = credit_limit
        existing.payment_term_id = payment_term_id
    session.flush()
    return partner_id


def ensure_salesperson(session: Session, *, employee_id: int, code: str) -> int:
    """Một nhân viên bán hàng với `id` cố định — bộ đếm tham chiếu đọc bảng này."""
    if session.get(Employee, employee_id) is None:
        session.add(
            Employee(
                id=employee_id,
                code=code,
                name=f"Nhân viên {code}",
                path=f"{employee_id}.",
            )
        )
        session.flush()
    return employee_id
