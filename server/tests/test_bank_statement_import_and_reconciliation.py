"""Nhập sao kê theo profile per-bank + đối chiếu (bước 13–14, lát 6D).

Trên PostgreSQL thật vì mọi lời hứa đều là lời hứa dữ liệu: trọn-hoặc-không
của lượt nhập, khóa băm chống nhập đúp, unique một-chứng-từ-một-dòng-mỗi-tài-
khoản (chuyển nội bộ khớp ở CẢ HAI sao kê), và khớp tự động chỉ khớp cái nó
chứng minh được (nhập nhằng để lại cho người).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from ket.kernel.bank_import.profile_models import (
    AmountSignRule,
    BankStatementProfile,
    StatementFileKind,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    BankStatementBalanceMismatchError,
    BankStatementDuplicateError,
    BankStatementImportInvalidError,
    BankStatementMatchInvalidError,
    BankStatementMatchStateError,
    BankStatementParseError,
)
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import (
    COMPANY_BANK_ACCOUNT_TABLE_NAME,
    CompanyBankAccount,
)
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.bank.models import (
    BankStatementLine,
    BankVoucherKind,
    StatementMatchKind,
)
from ket.modules.bank.reconciliation import (
    auto_match,
    candidates_for_line,
    match_line,
    reconciliation_summary,
    unmatch_line,
)
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.bank.statement_import import delete_statement, import_statement
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    seeded = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return seeded


@pytest.fixture
def bank_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code=f"SK-{context.branch_code}-1"
    )


@pytest.fixture
def second_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code=f"SK-{context.branch_code}-2"
    )


@pytest.fixture
def profile_id(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
) -> int:
    """Hồ sơ CSV hai cột nợ/có + cột số dư, của đúng ngân hàng VCB-TEST."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        account = session.get(CompanyBankAccount, bank_account)
        assert account is not None
        existing = session.scalar(
            select(BankStatementProfile).where(
                BankStatementProfile.bank_id == account.bank_id,
                BankStatementProfile.name == "CSV test 6D",
            )
        )
        if existing is not None:
            return existing.id
        profile = BankStatementProfile(
            bank_id=account.bank_id,
            name="CSV test 6D",
            file_kind=StatementFileKind.CSV,
            header_row=1,
            date_col="Ngay GD",
            date_format="%d/%m/%Y",
            debit_col="Ghi no",
            credit_col="Ghi co",
            ref_col="So CT",
            description_col="Dien giai",
            balance_col="So du",
            decimal_sep=".",
            thousand_sep=None,
            csv_delimiter=";",
        )
        session.add(profile)
        session.flush()
        return profile.id


def csv_of(rows: list[tuple[str, str, str, str, str, str]]) -> BytesIO:
    header = "Ngay GD;So CT;Dien giai;Ghi no;Ghi co;So du"
    body = "\n".join(";".join(row) for row in rows)
    return BytesIO(f"{header}\n{body}\n".encode())


def _import(
    session: Session,
    *,
    bank_account_id: int,
    profile_id: int,
    source: BytesIO,
    content_hash: str,
) -> object:
    return import_statement(
        session,
        bank_account_id=bank_account_id,
        profile_id=profile_id,
        source=source,
        file_name="sao-ke.csv",
        content_hash=content_hash,
        user_id=ACTOR_ID,
    )


def _post_credit_advice(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
    *,
    amount: int,
    posting_date: date = JAN_15,
    reference: str | None = None,
) -> object:
    service = BankVoucherService(session)
    voucher = service.create(
        BankVoucherIn(
            kind=BankVoucherKind.CREDIT_ADVICE,
            operation_code="thu-khac",
            bank_account_id=bank_account_id,
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code="VND",
            exchange_rate=Decimal(1),
            reference_no=reference,
            description="báo có đối chiếu",
            lines=(
                BankVoucherLineIn(
                    debit_account_id=accounts["112"],
                    credit_account_id=accounts["3381"],
                    amount_fc=Decimal(amount),
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher


def _post_payment_order(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
    *,
    amount: int,
    posting_date: date = JAN_15,
) -> object:
    service = BankVoucherService(session)
    voucher = service.create(
        BankVoucherIn(
            kind=BankVoucherKind.PAYMENT_ORDER,
            operation_code="chi-khac",
            bank_account_id=bank_account_id,
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="ủy nhiệm chi đối chiếu",
            lines=(
                BankVoucherLineIn(
                    debit_account_id=accounts["1381"],
                    credit_account_id=accounts["112"],
                    amount_fc=Decimal(amount),
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher


def test_import_writes_lines_balances_and_usage(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
    profile_id: int,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    rows = [
        ("15/01/2026", "FT-1", "khach chuyen", "", "700000", "1700000"),
        ("16/01/2026", "FT-2", "tra ncc", "300000", "", "1400000"),
    ]
    with unit_of_work(session_factory, scope) as session:
        before = usage_count_of(
            session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=bank_account
        )
        result = _import(
            session,
            bank_account_id=bank_account,
            profile_id=profile_id,
            source=csv_of(rows),
            content_hash="a" * 64,
        )
        statement = result.statement  # type: ignore[attr-defined]
        assert statement.statement_date == date(2026, 1, 16)
        # Số dư đầu suy từ dòng đầu: 1.700.000 − 700.000.
        assert statement.opening_balance == Decimal(1_000_000)
        assert statement.closing_balance == Decimal(1_400_000)
        lines = (
            session.execute(
                select(BankStatementLine)
                .where(BankStatementLine.statement_id == statement.id)
                .order_by(BankStatementLine.line_no)
            )
            .scalars()
            .all()
        )
        assert [(line.debit, line.credit) for line in lines] == [
            (Decimal(0), Decimal(700_000)),
            (Decimal(300_000), Decimal(0)),
        ]
        assert all(line.bank_account_id == bank_account for line in lines)
        assert all(line.match_kind == StatementMatchKind.UNMATCHED for line in lines)
        after = usage_count_of(
            session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=bank_account
        )
        assert after == before + 1

    # Nhập đúng tệp ấy lần nữa → 409, không nhân đôi sao kê.
    with pytest.raises(BankStatementDuplicateError):
        with unit_of_work(session_factory, scope) as session:
            _import(
                session,
                bank_account_id=bank_account,
                profile_id=profile_id,
                source=csv_of(rows),
                content_hash="a" * 64,
            )


def test_import_aborts_on_parse_issue_balance_mismatch_and_foreign_profile(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
    profile_id: int,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)

    with pytest.raises(BankStatementParseError) as parse_error:
        with unit_of_work(session_factory, scope) as session:
            _import(
                session,
                bank_account_id=bank_account,
                profile_id=profile_id,
                source=csv_of([("15/01/2026", "FT-9", "hong", "", "abc", "")]),
                content_hash="b" * 64,
            )
    assert parse_error.value.issues

    with pytest.raises(BankStatementBalanceMismatchError):
        with unit_of_work(session_factory, scope) as session:
            _import(
                session,
                bank_account_id=bank_account,
                profile_id=profile_id,
                source=csv_of(
                    [
                        ("15/01/2026", "FT-1", "ok", "", "700000", "1700000"),
                        # 1.700.000 − 300.000 phải là 1.400.000, tệp nói 999.
                        ("16/01/2026", "FT-2", "lech", "300000", "", "999"),
                    ]
                ),
                content_hash="c" * 64,
            )

    # Hồ sơ của ngân hàng khác → chặn từ cấu hình.
    with unit_of_work(session_factory, scope) as session:
        other_bank = Bank(code=f"OB-{context.branch_code}", name="Ngân hàng khác", path="0.")
        session.add(other_bank)
        session.flush()
        other_bank.path = f"{other_bank.id}."
        foreign = BankStatementProfile(
            bank_id=other_bank.id,
            name="CSV nhà khác",
            file_kind=StatementFileKind.CSV,
            header_row=1,
            date_col="Ngay GD",
            date_format="%d/%m/%Y",
            amount_col="Ghi co",
            sign_rule=AmountSignRule.SIGNED,
            decimal_sep=".",
            thousand_sep=None,
            csv_delimiter=";",
        )
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    with pytest.raises(BankStatementImportInvalidError):
        with unit_of_work(session_factory, scope) as session:
            _import(
                session,
                bank_account_id=bank_account,
                profile_id=foreign_id,
                source=csv_of([("15/01/2026", "FT-1", "ok", "", "700000", "")]),
                content_hash="d" * 64,
            )


def test_auto_match_amount_date_window_reference_and_ambiguity(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account: int,
    profile_id: int,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _post_credit_advice(
            session, context, accounts, bank_account, amount=700_000, reference="FT-1"
        )
        _post_payment_order(
            session, context, accounts, bank_account, amount=300_000, posting_date=date(2026, 1, 14)
        )
        # Hai báo có 200k giống hệt — nhập nhằng, phải để lại.
        _post_credit_advice(session, context, accounts, bank_account, amount=200_000)
        _post_credit_advice(session, context, accounts, bank_account, amount=200_000)

    with unit_of_work(session_factory, scope) as session:
        result = _import(
            session,
            bank_account_id=bank_account,
            profile_id=profile_id,
            source=csv_of(
                [
                    ("16/01/2026", "FT-1", "khach chuyen", "", "700000", "1700000"),
                    ("16/01/2026", "FT-2", "tra ncc", "300000", "", "1400000"),
                    ("17/01/2026", "FT-3", "khong co chung tu", "", "500000", "1900000"),
                    ("17/01/2026", "FT-4", "hai ung vien", "", "200000", "2100000"),
                ]
            ),
            content_hash="e" * 64,
        )
        statement_id = result.statement.id  # type: ignore[attr-defined]
        outcome = auto_match(session, statement_id=statement_id)
        assert outcome.matched == 2
        assert outcome.ambiguous_lines == 1
        assert outcome.unmatched_lines == 1

        lines = (
            session.execute(
                select(BankStatementLine)
                .where(BankStatementLine.statement_id == statement_id)
                .order_by(BankStatementLine.line_no)
            )
            .scalars()
            .all()
        )
        assert lines[0].match_kind == StatementMatchKind.AUTO
        assert lines[1].match_kind == StatementMatchKind.AUTO
        assert lines[2].match_kind == StatementMatchKind.UNMATCHED
        assert lines[3].match_kind == StatementMatchKind.UNMATCHED

        # Gợi ý cho dòng nhập nhằng: đúng hai ứng viên 200k.
        suggested = candidates_for_line(session, line_id=lines[3].id)
        assert len(suggested) == 2

        # Báo cáo lệch: dòng 500k chưa khớp + dòng 200k chưa khớp; sổ còn hai
        # báo có 200k chưa khớp.
        summary = reconciliation_summary(
            session, bank_account_id=bank_account, as_of=date(2026, 1, 31)
        )
        assert {line.id for line in summary.unmatched_statement_lines} == {
            lines[2].id,
            lines[3].id,
        }
        assert len(summary.unmatched_vouchers) == 2
        assert summary.statement_total_unmatched_in == Decimal(700_000)
        assert summary.statement_total_unmatched_out == Decimal(0)


def test_manual_match_rules_and_unmatch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account: int,
    profile_id: int,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        result = _import(
            session,
            bank_account_id=bank_account,
            profile_id=profile_id,
            # Ngày cách xa mọi chứng từ — khớp tay không giới hạn cửa sổ ngày.
            source=csv_of([("28/02/2026", "FT-90", "treo lau", "", "900000", "900000")]),
            content_hash="f" * 64,
        )
        line_id = (
            session.execute(
                select(BankStatementLine.id).where(
                    BankStatementLine.statement_id == result.statement.id  # type: ignore[attr-defined]
                )
            )
        ).scalar_one()

        draft_service = BankVoucherService(session)
        draft = draft_service.create(
            BankVoucherIn(
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation_code="thu-khac",
                bank_account_id=bank_account,
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chưa ghi sổ",
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["3381"],
                        amount_fc=Decimal(900_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        # Chưa ghi sổ → không khớp được.
        with pytest.raises(BankStatementMatchInvalidError):
            match_line(session, line_id=line_id, voucher_id=draft.id)

        wrong_amount = _post_credit_advice(session, context, accounts, bank_account, amount=800_000)
        with pytest.raises(BankStatementMatchInvalidError):
            match_line(session, line_id=line_id, voucher_id=wrong_amount.id)  # type: ignore[attr-defined]

        wrong_direction = _post_payment_order(
            session, context, accounts, bank_account, amount=900_000
        )
        with pytest.raises(BankStatementMatchInvalidError):
            match_line(session, line_id=line_id, voucher_id=wrong_direction.id)  # type: ignore[attr-defined]

        good = _post_credit_advice(session, context, accounts, bank_account, amount=900_000)
        match_line(session, line_id=line_id, voucher_id=good.id)  # type: ignore[attr-defined]
        line = session.get(BankStatementLine, line_id)
        assert line is not None
        assert line.match_kind == StatementMatchKind.MANUAL

        # Dòng đã khớp thì không khớp thêm; gỡ rồi mới khớp lại được.
        with pytest.raises(BankStatementMatchStateError):
            match_line(session, line_id=line_id, voucher_id=good.id)  # type: ignore[attr-defined]
        unmatch_line(session, line_id=line_id)
        with pytest.raises(BankStatementMatchStateError):
            unmatch_line(session, line_id=line_id)
        match_line(session, line_id=line_id, voucher_id=good.id)  # type: ignore[attr-defined]

        # Sao kê còn dòng đã khớp → không xóa được; gỡ khớp thì xóa được.
        with pytest.raises(BankStatementMatchStateError):
            delete_statement(session, statement_id=result.statement.id)  # type: ignore[attr-defined]
        unmatch_line(session, line_id=line_id)
        delete_statement(session, statement_id=result.statement.id)  # type: ignore[attr-defined]


def test_internal_transfer_matches_on_both_statements(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    bank_account: int,
    second_account: int,
    profile_id: int,
) -> None:
    """Chuyển nội bộ nằm trên HAI sao kê: tiền ra ở nguồn, tiền vào ở đích —
    cùng một chứng từ phải khớp được ở cả hai (unique index theo tài khoản)."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        service = BankVoucherService(session)
        transfer = service.create(
            BankVoucherIn(
                kind=BankVoucherKind.INTERNAL_TRANSFER,
                operation_code=None,
                bank_account_id=bank_account,
                counter_bank_account_id=second_account,
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chuyển nội bộ hai sao kê",
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["112"],
                        amount_fc=Decimal(250_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(transfer.id, user_id=ACTOR_ID)
        transfer_id = transfer.id

    with unit_of_work(session_factory, scope) as session:
        source_stmt = _import(
            session,
            bank_account_id=bank_account,
            profile_id=profile_id,
            source=csv_of([("15/01/2026", "CT-1", "chuyen di", "250000", "", "750000")]),
            content_hash="1" * 64,
        )
        target_stmt = _import(
            session,
            bank_account_id=second_account,
            profile_id=profile_id,
            source=csv_of([("15/01/2026", "CT-2", "nhan ve", "", "250000", "250000")]),
            content_hash="2" * 64,
        )
        source_outcome = auto_match(session, statement_id=source_stmt.statement.id)  # type: ignore[attr-defined]
        target_outcome = auto_match(session, statement_id=target_stmt.statement.id)  # type: ignore[attr-defined]
        assert source_outcome.matched == 1
        assert target_outcome.matched == 1

        matched = (
            session.execute(
                select(BankStatementLine).where(BankStatementLine.matched_voucher_id == transfer_id)
            )
            .scalars()
            .all()
        )
        assert {line.bank_account_id for line in matched} == {bank_account, second_account}
