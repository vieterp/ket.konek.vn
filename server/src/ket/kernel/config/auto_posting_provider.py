"""Đọc nghiệp vụ định khoản tự động của gói hiệu lực (FR-SYS-025) — lát 6A.

Cùng vai với `accounts_provider`: mọi nơi cần danh sách nghiệp vụ đi qua đây,
không nơi nào tự `SELECT` bảng `auto_posting_rules` — vì bước phân giải
purpose → số hiệu TK (qua `default_accounts`, có wildcard `'*'`) phải nằm ở
đúng một chỗ để form phiếu thu và form báo có không cho hai câu trả lời khác
nhau về cùng một nghiệp vụ.

Purpose thiếu trong `default_accounts` của gói **không phải lỗi** ở đường đọc
này: loader đã chặn purpose lạ lúc nạp gói, nên tới đây "thiếu" chỉ còn một
nghĩa — gói này cố ý không khai TK cho mục đích đó (TT133 không có TK bảo hiểm
chi tiết chẳng hạn), và câu trả lời đúng là "không điền sẵn bên đó" chứ không
phải 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import (
    DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE,
    DefaultAccount,
)
from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.config.auto_posting_models import AutoPostingRule


@dataclass(frozen=True)
class AutoPostingOperation:
    """Một nghiệp vụ đã phân giải xong — thứ form chứng từ hiển thị và điền sẵn.

    Mang **số hiệu** TK chứ không `id`: form tra chi tiết TK (tên, chiều bắt
    buộc) qua `/accounts` bằng chính số hiệu, và số hiệu là thứ kế toán viên
    đọc được ngay trên danh sách nghiệp vụ.
    """

    operation_code: str
    operation_name: str
    debit_account_code: str | None
    credit_account_code: str | None
    requires_partner: bool
    partner_kind: int | None
    display_order: int


def _purpose_map(session: Session, *, package_id: int) -> dict[tuple[str, str], str]:
    rows = session.execute(
        select(
            DefaultAccount.document_type, DefaultAccount.purpose, DefaultAccount.account_code
        ).where(DefaultAccount.package_id == package_id)
    ).all()
    return {(document_type, purpose): code for document_type, purpose, code in rows}


@dataclass(frozen=True)
class ResolvedOperations:
    """Kết quả phân giải: gói nào + danh sách nghiệp vụ của loại chứng từ.

    Mang `package_id` ra ngoài vì client cần so nó với `package_id` của
    `/accounts` — hai lượt tra trên hai endpoint phải cùng nói về một gói.
    """

    package_id: int
    items: tuple[AutoPostingOperation, ...]


def operations_for(
    session: Session, *, document_type: str, scheme: str, on_date: date
) -> ResolvedOperations:
    """Nghiệp vụ của một loại chứng từ trong gói hiệu lực tại `on_date`."""
    package = resolve_package(session, scheme=scheme, on_date=on_date)
    return operations_in_package(session, package_id=package.id, document_type=document_type)


def operations_in_package(
    session: Session, *, package_id: int, document_type: str
) -> ResolvedOperations:
    """Nghiệp vụ của một loại chứng từ trong MỘT gói cụ thể — lõi của
    `operations_for`, tách ra để test trỏ thẳng vào gói builtin mà không phụ
    thuộc gói nào đang thắng `resolve_package` trong dataset test.

    Một câu đọc `default_accounts` cho cả bảng thay vì `default_account()` từng
    purpose: danh sách nghiệp vụ dài hai chục dòng × hai purpose là bốn chục
    lượt tra — trên cùng một bảng bé mà request nào của form cũng gọi.
    Phân giải theo đúng luật của `accounts_provider.default_account`:
    `(document_type cụ thể, purpose)` trước, lùi về `('*', purpose)`.
    """
    purposes = _purpose_map(session, package_id=package_id)

    def account_of(purpose: str | None) -> str | None:
        if purpose is None:
            return None
        specific = purposes.get((document_type, purpose))
        if specific is not None:
            return specific
        return purposes.get((DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE, purpose))

    rules = (
        session.execute(
            select(AutoPostingRule)
            .where(AutoPostingRule.package_id == package_id)
            .where(AutoPostingRule.document_type == document_type)
            .order_by(AutoPostingRule.display_order, AutoPostingRule.operation_code)
        )
        .scalars()
        .all()
    )
    return ResolvedOperations(
        package_id=package_id,
        items=tuple(
            AutoPostingOperation(
                operation_code=rule.operation_code,
                operation_name=rule.operation_name,
                debit_account_code=account_of(rule.debit_purpose),
                credit_account_code=account_of(rule.credit_purpose),
                requires_partner=rule.requires_partner,
                partner_kind=rule.partner_kind,
                display_order=rule.display_order,
            )
            for rule in rules
        ),
    )
