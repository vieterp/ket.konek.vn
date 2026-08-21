"""Sheet ngân hàng của số dư ban đầu (kind 1, lát 6D — nợ hoãn từ 4C).

Ba lời hứa mới của lát:

* Sheet "Số dư ngân hàng" nhập được: dòng kind 1 mang `bank_account_id`, tiền
  tệ phải khớp tiền tệ TK ngân hàng, TK kế toán phải nhóm 112; TK 112 nhập ở
  sheet Số dư tài khoản chỉ còn là CẢNH BÁO (dữ liệu trước 6D vẫn đọc được).
* Carry-forward giữ nhóm 1 qua năm VÀ chia phát sinh 112 theo TK ngân hàng
  (qua `DepositMovementSource` module bank cài); phát sinh 112 không qua chứng
  từ tiền gửi (GLE gõ thẳng) ở lại nhóm 0 — không bịa chủ tài khoản.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.bank.models import BankVoucherKind
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.models import OpeningBalance, OpeningDetailKind
from ket.posting.opening_balances.template import ACCOUNT_SHEET, BANK_SHEET, build_opening_template
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_opening_balance_parsing import workbook_of
from test_opening_balances_carry_forward import carry_forward, ensure_fiscal_year
from test_opening_balances_import import opening_rows, run_opening_import

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
def vnd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> tuple[int, str]:
    code = f"OB-{context.branch_code}-VND"
    account_id = ensure_company_bank_account(session_factory, dataset_alpha, context, code=code)
    return account_id, code


@pytest.fixture
def usd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> tuple[int, str]:
    code = f"OB-{context.branch_code}-USD"
    account_id = ensure_company_bank_account(
        session_factory, dataset_alpha, context, code=code, currency_code="USD"
    )
    return account_id, code


def test_template_contains_bank_sheet() -> None:
    """Tệp mẫu tải về phải có sheet ngân hàng — hợp đồng FR-SYS-082."""
    from io import BytesIO

    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(build_opening_template()))
    assert "Số dư ngân hàng" in workbook.sheetnames
    header = [cell.value for cell in next(workbook["Số dư ngân hàng"].iter_rows(max_row=1))]
    assert header[0] == "Số tài khoản ngân hàng *"
    assert header[1] == "Số hiệu tài khoản *"


def test_bank_sheet_writes_kind1_rows_with_bank_account(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    vnd_account: tuple[int, str],
    usd_account: tuple[int, str],
    tmp_path: object,
) -> None:
    vnd_id, vnd_code = vnd_account
    usd_id, usd_code = usd_account
    report = run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,  # type: ignore[arg-type]
        workbook_of(
            {
                BANK_SHEET: [
                    [vnd_code, "112", None, None, None, None, 800_000, None],
                    [usd_code, "112", "USD", 25_000, 100, None, 2_500_000, None],
                ],
            }
        ),
    )
    assert report.committed, report.errors
    assert report.rows_by_kind == {OpeningDetailKind.BANK: 2}

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        rows = [
            row
            for row in opening_rows(session, context)
            if row.detail_kind == OpeningDetailKind.BANK
        ]
        by_account = {row.bank_account_id: row for row in rows}
        assert set(by_account) == {vnd_id, usd_id}
        assert by_account[vnd_id].debit == Decimal(800_000)
        assert by_account[vnd_id].currency_code == "VND"
        assert by_account[usd_id].debit_fc == Decimal(100)
        assert by_account[usd_id].debit == Decimal(2_500_000)
        assert by_account[usd_id].currency_code == "USD"


def test_bank_sheet_lookup_and_currency_validation(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    usd_account: tuple[int, str],
    tmp_path: object,
) -> None:
    """Mã lạ, sai tiền tệ, TK ngoài nhóm 112 — mỗi lỗi phải chỉ đúng dòng."""
    _, usd_code = usd_account
    report = run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,  # type: ignore[arg-type]
        workbook_of(
            {
                BANK_SHEET: [
                    ["KHONG-TON-TAI", "112", None, None, None, None, 100_000, None],
                    # TK USD nhưng dòng để trống loại tiền (= VND).
                    [usd_code, "112", None, None, None, None, 100_000, None],
                    # TK kế toán ngoài nhóm 112.
                    [usd_code, "111", "USD", 25_000, 4, None, 100_000, None],
                ],
            }
        ),
        commit=False,
    )
    codes = {error.code for error in report.errors}
    assert "opening.bank_account_unknown" in codes
    assert "opening.bank_account_currency_mismatch" in codes
    assert "opening.bank_account_not_deposit" in codes
    assert not report.committed


def test_deposit_account_on_plain_sheet_warns_but_commits(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: object,
) -> None:
    """Dữ liệu kiểu trước-6D (112 ở sheet Số dư tài khoản) vẫn ghi được —
    nhưng lượt kiểm phải nhắc chuyển sang sheet ngân hàng (BR-BNK-01)."""
    report = run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,  # type: ignore[arg-type]
        workbook_of({ACCOUNT_SHEET: [["112", None, None, None, None, 500_000, None]]}),
    )
    assert report.committed
    assert any(warning.code == "opening.deposit_on_account_sheet" for warning in report.warnings)


def test_carry_forward_splits_deposit_movement_by_bank_account(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    vnd_account: tuple[int, str],
    tmp_path: object,
) -> None:
    """Năm nguồn: dư đầu ngân hàng 800k + báo có 700k + GLE gõ thẳng 112 100k.

    Năm nhận phải ra: kind 1 (đúng TK ngân hàng) = 1.500k — dư đầu cộng phát
    sinh QUA chứng từ tiền gửi; phần GLE 100k ở lại kind 0 vì không ai biết nó
    thuộc tài khoản nào (docstring `DepositMovementSource`).
    """
    vnd_id, vnd_code = vnd_account
    report = run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,  # type: ignore[arg-type]
        workbook_of({BANK_SHEET: [[vnd_code, "112", None, None, None, None, 800_000, None]]}),
    )
    assert report.committed, report.errors

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        bank_service = BankVoucherService(session)
        voucher = bank_service.create(
            BankVoucherIn(
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation_code="thu-khac",
                bank_account_id=vnd_id,
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="báo có carry-forward",
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["3381"],
                        amount_fc=Decimal(700_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        bank_service.post(voucher.id, user_id=ACTOR_ID)

        journal = JournalVoucherService(session)
        gle = journal.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="GLE gõ thẳng 112",
                lines=(
                    JournalLineIn(account_id=accounts["112"], debit_fc=Decimal(100_000)),
                    JournalLineIn(account_id=accounts["3381"], credit_fc=Decimal(100_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        journal.post(gle.id, user_id=ACTOR_ID)

    with unit_of_work(session_factory, scope) as session:
        target_id = ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        result = carry_forward(session, dataset_alpha, context)
        assert result["target_fiscal_year_id"] == target_id

    with unit_of_work(session_factory, scope) as session:
        rows = (
            session.execute(
                select(OpeningBalance)
                .where(OpeningBalance.fiscal_year_id == target_id)
                .where(OpeningBalance.branch_id == context.branch_id)
                .where(OpeningBalance.ledger == Ledger.FINANCIAL)
                .where(OpeningBalance.account_id == accounts["112"])
            )
            .scalars()
            .all()
        )
        by_kind = {(row.detail_kind, row.bank_account_id): row for row in rows}
        bank_row = by_kind[(OpeningDetailKind.BANK, vnd_id)]
        assert bank_row.debit == Decimal(1_500_000)
        plain_row = by_kind[(OpeningDetailKind.ACCOUNT, None)]
        assert plain_row.debit == Decimal(100_000)


def test_bank_account_with_opening_rows_cannot_be_deleted(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    vnd_account: tuple[int, str],
    tmp_path: object,
) -> None:
    """FK RESTRICT: còn số dư treo thì không xóa được TK ngân hàng."""
    from sqlalchemy.exc import IntegrityError

    vnd_id, vnd_code = vnd_account
    report = run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,  # type: ignore[arg-type]
        workbook_of({BANK_SHEET: [[vnd_code, "112", None, None, None, None, 250_000, None]]}),
    )
    assert report.committed

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, scope) as session:
            account = session.get(CompanyBankAccount, vnd_id)
            assert account is not None
            session.delete(account)
            session.flush()
