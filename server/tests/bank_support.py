"""Khung dựng bối cảnh cho test phân hệ Ngân hàng (lát 6C).

Bồi lên `cash_book_support` cùng lối: gói `TT99-TEST` thắng `resolve_package`
trong dataset test nên nghiệp vụ BC/UNC/SEC phải được gieo vào CHÍNH gói đó,
idempotent để nhiều tệp test dùng chung dataset không giẫm nhau. Kèm hai danh
mục mà chứng từ tiền gửi bắt buộc: một ngân hàng (`banks`) và tài khoản ngân
hàng doanh nghiệp (`company_bank_accounts`, FR-BNK-002).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.persistence.unit_of_work import unit_of_work
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

_BANK_RULES: tuple[tuple[str, str, str, str | None, str | None, bool, int | None, int], ...] = (
    # (document_type, code, name, debit_purpose, credit_purpose, requires_partner, partner_kind, order)
    ("BC", "thu-khac", "Thu khác", None, None, False, None, 1),
    ("BC", "thu-no-khach-hang", "Khách hàng trả nợ", None, None, True, 0, 2),
    ("UNC", "chi-khac", "Chi khác", None, None, False, None, 1),
    ("UNC", "tra-no-ncc", "Trả nợ nhà cung cấp", None, None, True, 1, 2),
    ("SEC", "chi-khac", "Chi khác bằng séc", None, None, False, None, 1),
    ("SEC", "tra-no-ncc", "Trả nợ nhà cung cấp bằng séc", None, None, True, 1, 2),
)


def seed_bank_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> None:
    """Gieo nghiệp vụ BC/UNC/SEC vào gói test — idempotent theo khóa tự nhiên."""
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        for document_type, code, name, debit, credit, requires, partner_kind, order in _BANK_RULES:
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


def ensure_company_bank_account(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    *,
    code: str,
    currency_code: str | None = None,
    branch_id: int | None = None,
) -> int:
    """Một tài khoản ngân hàng doanh nghiệp (kèm ngân hàng cha) — trả `id`.

    `currency_code=None` = đồng hạch toán (VND), đúng ngữ nghĩa cột.
    `branch_id=None` = dùng chung toàn công ty (`MasterDataRow.branch_id`);
    truyền một chi nhánh khi bài kiểm cần sao kê BỊ cô lập (lát 6G-1).
    Idempotent theo `code` để các tệp test dùng chung dataset không giẫm nhau.
    """
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        existing = session.scalar(select(CompanyBankAccount).where(CompanyBankAccount.code == code))
        if existing is not None:
            return existing.id
        bank = session.scalar(select(Bank).where(Bank.code == "VCB-TEST"))
        if bank is None:
            bank = Bank(code="VCB-TEST", name="Ngân hàng test", path="0.")
            session.add(bank)
            session.flush()
            bank.path = f"{bank.id}."
            session.flush()
        account = CompanyBankAccount(
            code=code,
            name=f"TK ngân hàng {code}",
            path="0.",
            bank_id=bank.id,
            currency_code=currency_code,
            branch_id=branch_id,
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        session.flush()
        return account.id
