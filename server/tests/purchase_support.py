"""Khung dựng bối cảnh cho test phân hệ Mua hàng (lát 7B).

Bồi lên `posting_support.seed_posting_context` cùng lối `cash_book_support`:
gói `TT99-TEST` thắng `resolve_package` trong dataset test, nên TK hàng hóa /
thuế đầu vào, purpose chênh lệch tỷ giá và nghiệp vụ `PUR` (FR-SYS-025) phải
được gieo vào CHÍNH gói đó — idempotent theo khóa tự nhiên để nhiều tệp test
dùng chung dataset không giẫm nhau (kể cả giẫm lên phần `cash_book_support`
gieo: cùng 515/635 và cùng cặp `(*, fx_gain/fx_loss)`).

`1331` ở gói test không theo dõi vật tư — cùng với gói builtin từ 0026 (thuế
đầu vào của dịch vụ và chi phí mua hàng không có vật tư để điền).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DefaultAccount
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.item import Item, ItemNature
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.master_data.models.project import Project
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.persistence.unit_of_work import unit_of_work
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

_EXTRA_ACCOUNT_SPECS: tuple[tuple[str, str, int, list[str] | None], ...] = (
    # (code, name, balance_nature, detail_tracking)
    ("156", "Hàng hóa", BalanceNature.DEBIT, None),
    ("1331", "Thuế GTGT được khấu trừ của hàng hóa, dịch vụ", BalanceNature.DEBIT, None),
    # TK phải trả THỨ HAI theo dõi NCC — để kiểm "đối trừ vào khoản nợ nằm trên
    # TK khác" mà không phải bịa một TK không theo dõi đối tác.
    ("3388", "Phải trả, phải nộp khác", BalanceNature.DUAL, ["vendor"]),
    ("515", "Doanh thu hoạt động tài chính", BalanceNature.NONE, None),
    ("635", "Chi phí tài chính", BalanceNature.NONE, None),
)

_DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (("fx_gain", "515"), ("fx_loss", "635"))

_RULES: tuple[tuple[str, str, str, bool, int | None, int], ...] = (
    # (document_type, code, name, requires_partner, partner_kind, order)
    ("PUR", "mua-hang-hoa", "Mua hàng hóa nhập kho", True, 1, 1),
    ("PUR", "mua-dich-vu", "Mua dịch vụ", True, 1, 2),
    ("PUR", "tra-lai-hang-mua", "Trả lại hàng mua", True, 1, 3),
    # Nghiệp vụ gói khai sai loại đối tác — để kiểm module từ chối.
    ("PUR", "mua-cua-khach", "Nghiệp vụ khai nhầm cho khách hàng", True, 0, 9),
)


def seed_purchase_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo TK + purpose + nghiệp vụ mua vào gói test; trả bảng số hiệu → id."""
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


def ensure_vendor(
    session: Session,
    *,
    partner_id: int,
    code: str,
    credit_limit: Decimal | None = None,
    payment_term_id: int | None = None,
    tax_code: str | None = None,
) -> int:
    """Một nhà cung cấp với `id` cố định — idempotent để fixture module dùng lại.

    `tax_code` để trống ở phần lớn bài: nó chỉ có nghĩa với lượt nạp hóa đơn
    đầu vào (7F-2b), nơi mã số thuế là đường duy nhất nối người bán trên tờ hóa
    đơn với một dòng danh mục.
    """
    existing = session.get(Partner, partner_id)
    if existing is None:
        session.add(
            Partner(
                id=partner_id,
                code=code,
                name=f"Nhà cung cấp {code}",
                path=f"{partner_id}.",
                is_vendor=True,
                credit_limit=credit_limit,
                payment_term_id=payment_term_id,
                tax_code=tax_code,
            )
        )
    else:
        existing.credit_limit = credit_limit
        existing.payment_term_id = payment_term_id
        # `tax_code` chỉ ghi khi lượt gọi **có** nói tới nó, khác hai trường
        # trên: một tệp test gọi lại `ensure_vendor` cho cùng `partner_id` mà
        # không quan tâm mã số thuế sẽ xóa mất mã mà tệp khác vừa đặt — đúng
        # khuôn bẫy "bài test phụ thuộc thứ tự tệp".
        if tax_code is not None:
            existing.tax_code = tax_code
    session.flush()
    return partner_id


def ensure_payment_term(session: Session, *, term_id: int, code: str, due_days: int) -> int:
    existing = session.get(PaymentTerm, term_id)
    if existing is None:
        session.add(
            PaymentTerm(
                id=term_id,
                code=code,
                name=f"Nợ {due_days} ngày",
                path=f"{term_id}.",
                due_days=due_days,
            )
        )
    else:
        existing.due_days = due_days
    session.flush()
    return term_id


def ensure_buyer(session: Session, *, employee_id: int, code: str) -> int:
    """Một nhân viên mua hàng với `id` cố định — cùng khuôn `ensure_salesperson`.

    Bộ đếm tham chiếu đọc bảng này, nên `purchase_invoices.buyer_id` phải trỏ
    một dòng có thật để lượt tạo hóa đơn không nhích bộ đếm của một id không ai.
    """
    if session.get(Employee, employee_id) is None:
        session.add(
            Employee(id=employee_id, code=code, name=f"Nhân viên {code}", path=f"{employee_id}.")
        )
        session.flush()
    return employee_id


def ensure_unit(session: Session, *, unit_id: int, code: str) -> int:
    if session.get(UnitOfMeasure, unit_id) is None:
        session.add(UnitOfMeasure(id=unit_id, code=code, name=code, path=f"{unit_id}."))
        session.flush()
    return unit_id


def ensure_item(session: Session, *, item_id: int, code: str, unit_id: int | None = None) -> int:
    """Một dòng vật tư hàng hóa với `id` cố định.

    `nature = goods` để dòng này giống hàng mua thật; báo cáo mua hàng không đọc
    `nature`, nhưng một vật tư không tính chất là thứ phase 8 sẽ bỏ qua và bài
    test sẽ đúng vì lý do khác lý do nó viết ra.
    """
    if session.get(Item, item_id) is None:
        session.add(
            Item(
                id=item_id,
                code=code,
                name=f"Vật tư {code}",
                path=f"{item_id}.",
                nature=ItemNature.GOODS,
                base_unit_id=unit_id,
            )
        )
        session.flush()
    return item_id


def ensure_project(session: Session, *, project_id: int, code: str) -> int:
    if session.get(Project, project_id) is None:
        session.add(
            Project(id=project_id, code=code, name=f"Công trình {code}", path=f"{project_id}.")
        )
        session.flush()
    return project_id
