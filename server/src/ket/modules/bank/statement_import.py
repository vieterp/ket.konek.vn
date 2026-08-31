"""Nhập sao kê ngân hàng từ tệp CSV/XLSX theo hồ sơ per-bank (bước 13, lát 6D).

Bộ đọc (RT-26) nằm ở `kernel/bank_import` từ 3C-2 — nó trả lời "tệp này nói
gì"; module này trả lời phần còn lại của FR-BNK-032: tệp ấy thuộc TK ngân hàng
nào, đã nhập chưa, số dư có tự nhất quán không, và ghi xuống
`bank_statements`/`bank_statement_lines` để đối chiếu (bước 14) làm việc.

MT940 **hoãn có chủ đích** (quyết định 6D cùng user): chưa có một tệp MT940
thật từ ngân hàng VN để đối chiếu, và doctrine `kernel/bank_import` nói thẳng
dựng bộ đọc theo trí tưởng tượng là dựng sai; `parse_statement` đã từ chối
`file_kind = mt940` bằng lỗi có thông điệp.

Nhập **trọn-hoặc-không**: một dòng hỏng là cả lượt dừng (trả toàn bộ lỗi) —
nửa sao kê làm báo cáo chênh lệch (FR-BNK-031) nói dối. Dòng ghi bằng Core
executemany (khuôn H84: một dòng nhật ký `IMPORTED` cho cả lượt, không phải
500 dòng "rỗng → giá trị").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import IO
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.bank_import.profile_models import BankStatementProfile
from ket.kernel.bank_import.profile_parser import StatementLine, parse_statement
from ket.kernel.errors import (
    BankStatementBalanceMismatchError,
    BankStatementDuplicateError,
    BankStatementImportInvalidError,
    BankStatementMatchStateError,
    BankStatementNotFoundError,
    BankStatementParseError,
)
from ket.kernel.master_data.models.company_bank_account import (
    COMPANY_BANK_ACCOUNT_TABLE_NAME,
    CompanyBankAccount,
)
from ket.kernel.master_data.usage import record_use
from ket.kernel.money import ZERO
from ket.modules.bank.models import (
    BankStatement,
    BankStatementLine,
    StatementMatchKind,
)


@dataclass(frozen=True)
class ImportedStatement:
    """Kết quả một lượt nhập — con số router trả cho màn hình đối chiếu."""

    statement: BankStatement
    line_count: int
    total_credit: Decimal
    total_debit: Decimal


def require_bank_account(session: Session, bank_account_id: int) -> CompanyBankAccount:
    """TK ngân hàng phải có thật, là tài khoản cụ thể và còn theo dõi."""
    account = session.get(CompanyBankAccount, bank_account_id)
    if account is None:
        raise BankStatementImportInvalidError(
            "Tài khoản ngân hàng không có trong danh mục", bank_account_id=bank_account_id
        )
    if account.is_group:
        raise BankStatementImportInvalidError(
            "Đây là một nhóm — chọn một tài khoản ngân hàng cụ thể",
            bank_account_id=bank_account_id,
        )
    if not account.is_active:
        raise BankStatementImportInvalidError(
            "Tài khoản ngân hàng đã ngừng theo dõi", bank_account_id=bank_account_id
        )
    return account


def import_statement(
    session: Session,
    *,
    bank_account_id: int,
    profile_id: int,
    source: IO[bytes],
    file_name: str,
    content_hash: str,
    user_id: int,
) -> ImportedStatement:
    """Đọc tệp theo hồ sơ và ghi sao kê + dòng, trong transaction của người gọi."""
    account = require_bank_account(session, bank_account_id)
    profile = session.get(BankStatementProfile, profile_id)
    if profile is None:
        raise BankStatementImportInvalidError(
            "Hồ sơ định dạng sao kê không tồn tại", profile_id=profile_id
        )
    if profile.bank_id != account.bank_id:
        # Hồ sơ của nhà băng khác vẫn ĐỌC được tệp (cột trùng tên là chuyện
        # thường) — nhưng số ra sẽ sai kiểu im lặng đúng như profile_models
        # cảnh báo, nên chặn từ cấu hình chứ không đợi số lệch.
        raise BankStatementImportInvalidError(
            "Hồ sơ định dạng thuộc ngân hàng khác với ngân hàng của tài khoản",
            profile_id=profile_id,
            bank_account_id=bank_account_id,
        )

    duplicate = session.execute(
        select(func.count())
        .select_from(BankStatement)
        .where(
            BankStatement.bank_account_id == bank_account_id,
            BankStatement.content_hash == content_hash,
        )
    ).scalar_one()
    if duplicate:
        raise BankStatementDuplicateError(
            "Đúng tệp này đã nhập cho tài khoản này rồi — xóa sao kê cũ nếu muốn nhập lại",
            content_hash=content_hash,
        )

    result = parse_statement(source, profile)
    if result.issues:
        raise BankStatementParseError(
            "Tệp có dòng không đọc được theo hồ sơ định dạng — sửa tệp hoặc hồ sơ rồi nhập lại",
            issues=[
                {
                    "row": issue.row_number,
                    "column": issue.column,
                    "code": issue.code,
                    "message": issue.message,
                    "value": issue.value,
                }
                for issue in result.issues
            ],
        )
    if not result.lines:
        raise BankStatementImportInvalidError("Tệp không có dòng giao dịch nào")

    opening_balance, closing_balance = _verify_running_balance(result.lines)

    statement = BankStatement(
        bank_account_id=bank_account_id,
        # Chi nhánh đi theo TÀI KHOẢN, không theo người nhập — xem
        # `statement_branch`; `NULL` = tài khoản dùng chung toàn công ty.
        branch_id=account.branch_id,
        statement_date=max(line.booked_on for line in result.lines),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        profile_id=profile.id,
        content_hash=content_hash,
        imported_by=user_id,
    )
    session.add(statement)
    try:
        session.flush()
    except IntegrityError as error:
        # Lượt song song vừa commit đúng tệp này — phép kiểm trùng ở trên
        # không thấy dòng chưa commit, unique index mới là người canh thật
        # (review 6D, H-2); dịch thành cùng 409 với đường tuần tự.
        raise BankStatementDuplicateError(
            "Đúng tệp này đã nhập cho tài khoản này rồi — xóa sao kê cũ nếu muốn nhập lại",
            content_hash=content_hash,
        ) from error

    session.execute(
        insert(BankStatementLine),
        [
            {
                "statement_id": statement.id,
                "bank_account_id": bank_account_id,
                "branch_id": account.branch_id,
                "line_no": line.row_number,
                "txn_date": line.booked_on,
                "reference_no": line.reference,
                "description": line.description,
                "debit": -line.amount if line.amount < ZERO else ZERO,
                "credit": line.amount if line.amount > ZERO else ZERO,
                "match_kind": StatementMatchKind.UNMATCHED,
            }
            for line in result.lines
        ],
    )
    record_use(session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=bank_account_id)
    record_action(
        session,
        entity_type=BankStatement.__tablename__,
        entity_id=str(statement.id),
        action=AuditAction.IMPORTED,
        branch_id=None,
        new_values={
            "bank_account_id": bank_account_id,
            "profile_id": profile.id,
            "file_name": file_name,
            "content_hash": content_hash,
            "lines": len(result.lines),
        },
    )
    total_credit = sum((line.amount for line in result.lines if line.amount > ZERO), ZERO)
    total_debit = sum((-line.amount for line in result.lines if line.amount < ZERO), ZERO)
    return ImportedStatement(
        statement=statement,
        line_count=len(result.lines),
        total_credit=total_credit,
        total_debit=total_debit,
    )


def _verify_running_balance(
    lines: tuple[StatementLine, ...],
) -> tuple[Decimal | None, Decimal | None]:
    """`(số dư đầu, số dư cuối)` suy từ cột số dư của tệp, nếu có.

    Hồ sơ có `balance_col` là lời hứa kiểm chéo được (docstring
    `profile_models.balance_col`): số dư dòng trước + phát sinh = số dư dòng
    sau, cho TỪNG dòng có số dư. Lệch một chỗ nào = hồ sơ trỏ nhầm cột (số
    tiền ↔ số dư là ca hỏng im lặng) hoặc tệp bị cắt xén — cả hai đều phải
    dừng lượt nhập chứ không ghi một sao kê tự mâu thuẫn.

    Ô số dư ĐƯỢC PHÉP THƯA (review 6D, H-1): nhiều export ngân hàng thật chỉ
    in số dư ở dòng cuối mỗi ngày. Phát sinh của các dòng không mang số dư
    phải cộng dồn vào kỳ vọng của dòng-có-số-dư kế tiếp — bỏ qua chúng là vừa
    từ chối tệp hợp lệ, vừa nhận tệp mất đúng chừng ấy tiền.

    Tệp không mang số dư (mọi `balance` là `None`) thì trả `(None, None)` —
    không bịa số 0 (docstring `BankStatement`).
    """
    running: Decimal | None = None
    opening: Decimal | None = None
    pending = ZERO
    """Tổng phát sinh của các dòng KHÔNG mang số dư kể từ mốc gần nhất."""
    for line in lines:
        pending += line.amount
        if line.balance is None:
            continue
        if running is None:
            # Dư đầu sao kê = số dư mốc đầu tiên trừ MỌI phát sinh trước đó
            # (kể cả các dòng thưa đứng trước mốc — H-1: mốc đầu không nhất
            # thiết là dòng 1).
            opening = line.balance - pending
        else:
            expected = running + pending
            if line.balance != expected:
                raise BankStatementBalanceMismatchError(
                    "Cột số dư không khớp phát sinh cộng dồn — kiểm tra hồ sơ định dạng "
                    "(cột số tiền/số dư) hoặc tệp",
                    row=line.row_number,
                    expected=str(expected),
                    found=str(line.balance),
                )
        running = line.balance
        pending = ZERO
    return opening, running


def require_statement(session: Session, statement_id: UUID) -> BankStatement:
    statement = session.get(BankStatement, statement_id)
    if statement is None:
        raise BankStatementNotFoundError("Không tìm thấy sao kê", statement_id=str(statement_id))
    return statement


def delete_statement(session: Session, *, statement_id: UUID) -> None:
    """Xóa một sao kê chưa có dòng nào khớp (đường nhập-lại sau khi nhập nhầm).

    Dòng đã khớp giữ tham chiếu chứng từ (`RESTRICT` trên FK) — bắt gỡ khớp
    trước để thao tác gỡ có nhật ký, thay vì một lượt xóa im lặng làm chứng từ
    "chưa từng được đối chiếu".
    """
    statement = require_statement(session, statement_id)
    # Khóa TOÀN BỘ dòng trước khi đếm (review 6D, H-4): một `match_line` đang
    # giữ FOR UPDATE chưa commit thì COUNT kiểu READ COMMITTED thấy 0 — xóa sẽ
    # nuốt im lặng đúng dòng vừa khớp. Khóa ở đây xếp hàng sau lượt khớp, và
    # đếm lại SAU khóa thấy sự thật đã commit.
    locked = (
        session.execute(
            select(BankStatementLine)
            .where(BankStatementLine.statement_id == statement_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    matched = sum(1 for line in locked if line.matched_voucher_id is not None)
    if matched:
        raise BankStatementMatchStateError(
            "Sao kê còn dòng đã khớp chứng từ — gỡ khớp trước khi xóa",
            matched_lines=int(matched),
        )
    bank_account_id = statement.bank_account_id
    session.execute(delete(BankStatementLine).where(BankStatementLine.statement_id == statement_id))
    session.delete(statement)
    session.flush()
    record_use(
        session,
        entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME,
        entity_id=bank_account_id,
        delta=-1,
    )
