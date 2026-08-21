"""Vòng đời chứng từ tiền gửi trên PostgreSQL thật (lát 6C).

Kiểm những bất biến RIÊNG của module ngân hàng — luật ghi sổ chung đã có
`test_posting_engine_flow.py`, đối trừ + FX ở `test_bank_settlement_and_fx.py`:

* Cất cấp số `BC{YY}-`/`UNC{YY}-`/`SEC{YY}-`/`CTNB{YY}-` theo loại; nghiệp vụ
  phải thuộc gói hiệu lực cho ĐÚNG loại chứng từ (FR-SYS-025, nợ 6A: séc có
  bộ nghiệp vụ riêng).
* Tiền tệ chứng từ phải khớp tiền tệ tài khoản ngân hàng (FR-BNK-002);
  chuyển nội bộ đòi hai tài khoản cùng tiền tệ.
* Mapper: bên tiền (TK nhóm 111/112) không nhận chiều khi bên kia là bên
  nghiệp vụ.
* Bộ đếm `master_data_usage` đếm cả `company_bank_accounts` (nợ 6A) — nhích
  khi cất, đổi khi sửa, lùi khi xóa.
* Loại chứng từ và chi nhánh bất biến khi sửa.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError, VoucherBranchImmutableError
from ket.kernel.master_data.models.company_bank_account import COMPANY_BANK_ACCOUNT_TABLE_NAME
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.bank.models import BankVoucherKind
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.posting.engine.models import GlPosting, Ledger
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    seeded = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return seeded


@pytest.fixture(scope="module")
def vnd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code="0011-BANK-VND"
    )


@pytest.fixture(scope="module")
def usd_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code="0012-BANK-USD", currency_code="USD"
    )


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _credit_advice(
    context: PostingContext,
    accounts: dict[str, int],
    bank_account_id: int,
    *,
    operation: str = "thu-khac",
    amount: int = 700_000,
    partner: tuple[PartnerKind, int] | None = None,
    line_partner: tuple[PartnerKind, int] | None = None,
) -> BankVoucherIn:
    partner_kind, partner_id = partner if partner else (None, None)
    lp_kind, lp_id = line_partner if line_partner else (None, None)
    return BankVoucherIn(
        kind=BankVoucherKind.CREDIT_ADVICE,
        operation_code=operation,
        bank_account_id=bank_account_id,
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="VND",
        exchange_rate=Decimal(1),
        partner_kind=partner_kind,
        partner_id=partner_id,
        reference_no="FT2026-000123",
        description="báo có test",
        lines=(
            BankVoucherLineIn(
                debit_account_id=accounts["112"],
                credit_account_id=accounts["131"],
                amount_fc=Decimal(amount),
                partner_kind=lp_kind,
                partner_id=lp_id,
            ),
        ),
    )


def _transfer(
    context: PostingContext,
    accounts: dict[str, int],
    source_id: int,
    counter_id: int,
    *,
    amount: int = 250_000,
) -> BankVoucherIn:
    return BankVoucherIn(
        kind=BankVoucherKind.INTERNAL_TRANSFER,
        operation_code=None,
        bank_account_id=source_id,
        counter_bank_account_id=counter_id,
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="chuyển nội bộ test",
        lines=(
            BankVoucherLineIn(
                debit_account_id=accounts["112"],
                credit_account_id=accounts["112"],
                amount_fc=Decimal(amount),
            ),
        ),
    )


def test_create_numbers_by_kind_posts_and_strips_money_side_dimensions(
    run: Runner, context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(
            _credit_advice(
                context,
                accounts,
                vnd_account,
                operation="thu-no-khach-hang",
                partner=(PartnerKind.CUSTOMER, 811),
                line_partner=(PartnerKind.CUSTOMER, 811),
            ),
            user_id=ACTOR_ID,
        )
        assert voucher.voucher_no.startswith("BC26-")
        assert voucher.document_type == "BC"
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
            )
            >= 1
        )

        service.post(voucher.id, user_id=ACTOR_ID)
        postings = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL)
                .order_by(GlPosting.line_no)
            )
            .scalars()
            .all()
        )
        assert len(postings) == 2
        bank_side = next(row for row in postings if row.account_id == accounts["112"])
        partner_side = next(row for row in postings if row.account_id == accounts["131"])
        # Bên tiền không nhận chiều; bên nghiệp vụ giữ đối tác (thẻ công nợ
        # phase 7 không đếm đôi).
        assert bank_side.partner_id is None
        assert partner_side.partner_id == 811
        return None

    run(work)


def test_operation_must_belong_to_package_for_the_document_type(
    run: Runner, context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _credit_advice(context, accounts, vnd_account, operation="khong-ton-tai"),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "bank.operation_unknown"

        # Nghiệp vụ PT không tự lọt sang BC — danh sách tra theo document_type.
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _credit_advice(context, accounts, vnd_account, operation="thua-quy-kiem-ke"),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "bank.operation_unknown"

        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _credit_advice(context, accounts, vnd_account, operation="thu-no-khach-hang"),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "bank.operation_partner_required"
        return None

    run(work)


def test_cheque_uses_its_own_operation_list_and_numbering(
    run: Runner, context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(
            BankVoucherIn(
                kind=BankVoucherKind.CHEQUE,
                operation_code="chi-khac",
                bank_account_id=vnd_account,
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                cheque_no="AC-000045",
                cheque_date=JAN_15,
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["642"],
                        credit_account_id=accounts["112"],
                        amount_fc=Decimal(120_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        assert voucher.voucher_no.startswith("SEC26-")
        assert voucher.document_type == "SEC"
        return None

    run(work)


def test_voucher_currency_must_match_bank_account_currency(
    run: Runner, context: PostingContext, accounts: dict[str, int], usd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        with pytest.raises(PostingValidationError) as caught:
            service.create(_credit_advice(context, accounts, usd_account), user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "bank.account_currency_mismatch"
        return None

    run(work)


def test_internal_transfer_posts_counts_both_accounts_and_requires_same_currency(
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    vnd_account: int,
    usd_account: int,
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        second_vnd = None  # tài khoản đích cùng tiền tệ tạo ở fixture module khác? — tạo tại chỗ
        from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount

        second_row = session.scalar(
            select(CompanyBankAccount).where(CompanyBankAccount.code == "0013-BANK-VND-2")
        )
        if second_row is None:
            source = session.get(CompanyBankAccount, vnd_account)
            assert source is not None
            second_row = CompanyBankAccount(
                code="0013-BANK-VND-2",
                name="TK ngân hàng thứ hai",
                path="0.",
                bank_id=source.bank_id,
            )
            session.add(second_row)
            session.flush()
            second_row.path = f"{second_row.id}."
            session.flush()
        second_vnd = second_row.id

        voucher = service.create(
            _transfer(context, accounts, vnd_account, second_vnd), user_id=ACTOR_ID
        )
        assert voucher.voucher_no.startswith("CTNB26-")
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=second_vnd
            )
            >= 1
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        postings = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL)
            )
            .scalars()
            .all()
        )
        assert {row.account_id for row in postings} == {accounts["112"]}
        assert sum(row.debit for row in postings) == sum(row.credit for row in postings)

        with pytest.raises(PostingValidationError) as caught:
            service.create(_transfer(context, accounts, vnd_account, usd_account), user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "bank.transfer_currency_mismatch"
        return None

    run(work)


def test_kind_and_branch_immutable_on_update(
    run: Runner, context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(_credit_advice(context, accounts, vnd_account), user_id=ACTOR_ID)

        changed_kind = _credit_advice(context, accounts, vnd_account).model_copy(
            update={"kind": BankVoucherKind.PAYMENT_ORDER}
        )
        with pytest.raises(PostingValidationError) as caught:
            service.update(
                voucher.id,
                changed_kind,
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "bank.kind_immutable"

        changed_branch = _credit_advice(context, accounts, vnd_account).model_copy(
            update={"branch_id": context.branch_id + 999}
        )
        with pytest.raises(VoucherBranchImmutableError):
            service.update(
                voucher.id,
                changed_branch,
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        return None

    run(work)


def test_delete_returns_usage_counters(
    run: Runner, context: PostingContext, accounts: dict[str, int], vnd_account: int
) -> None:
    def work(session: Session) -> object:
        service = BankVoucherService(session)
        before = usage_count_of(
            session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
        )
        voucher = service.create(_credit_advice(context, accounts, vnd_account), user_id=ACTOR_ID)
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
            )
            == before + 1
        )
        service.delete(voucher.id)
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
            )
            == before
        )
        return None

    run(work)


def test_update_moves_usage_counters_and_matches_the_integrity_check(
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    vnd_account: int,
) -> None:
    """Review 6C M-3: sửa chứng từ đổi TK ngân hàng phải trừ counter cũ, cộng
    counter mới (không đếm trùng), và nhánh bank của `usage_counter_accurate`
    phải cân — chạy trọn câu SQL check, 0 dòng lệch cho các entity của test."""

    def work(session: Session) -> object:
        from sqlalchemy import text

        from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
        from ket.posting.integrity.checks.registry import check_of

        source = session.get(CompanyBankAccount, vnd_account)
        assert source is not None
        other_row = session.scalar(
            select(CompanyBankAccount).where(CompanyBankAccount.code == "0014-BANK-VND-3")
        )
        if other_row is None:
            other_row = CompanyBankAccount(
                code="0014-BANK-VND-3",
                name="TK ngân hàng thứ ba",
                path="0.",
                bank_id=source.bank_id,
            )
            session.add(other_row)
            session.flush()
            other_row.path = f"{other_row.id}."
            session.flush()
        other = other_row.id

        service = BankVoucherService(session)
        before_old = usage_count_of(
            session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
        )
        before_new = usage_count_of(
            session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=other
        )
        voucher = service.create(_credit_advice(context, accounts, vnd_account), user_id=ACTOR_ID)
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
            )
            == before_old + 1
        )

        moved = _credit_advice(context, accounts, other)
        service.update(
            voucher.id, moved, expected_row_version=voucher.row_version, user_id=ACTOR_ID
        )
        assert (
            usage_count_of(
                session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=vnd_account
            )
            == before_old
        )
        assert (
            usage_count_of(session, entity_type=COMPANY_BANK_ACCOUNT_TABLE_NAME, entity_id=other)
            == before_new + 1
        )

        # Nguồn đối chiếu phải ĐỒNG Ý với bộ đếm — mất nhánh bank trong câu SQL
        # (mutation M15) hay đếm trùng khi sửa (M8) đều lộ ra ở đây.
        sql = check_of("usage_counter_accurate").sql()
        mismatches = session.execute(text(sql), {"branch_id": context.branch_id}).all()
        flagged = {
            (row.entity_type, row.entity_id)
            for row in mismatches
            if row.entity_type == COMPANY_BANK_ACCOUNT_TABLE_NAME
            and row.entity_id in (vnd_account, other)
        }
        assert flagged == set()
        return None

    run(work)


def test_schema_rejects_malformed_transfer_and_cheque_combinations() -> None:
    base: dict[str, object] = {
        "bank_account_id": 1,
        "branch_id": 1,
        "document_date": JAN_15,
        "posting_date": JAN_15,
        "currency_code": "VND",
        "lines": (
            BankVoucherLineIn(debit_account_id=1, credit_account_id=2, amount_fc=Decimal(1)),
        ),
    }
    with pytest.raises(ValidationError):
        # Chuyển nội bộ không có nghiệp vụ.
        BankVoucherIn(
            kind=BankVoucherKind.INTERNAL_TRANSFER,
            operation_code="chi-khac",
            counter_bank_account_id=2,
            **base,
        )
    with pytest.raises(ValidationError):
        # Chuyển nội bộ bắt buộc TK đích.
        BankVoucherIn(kind=BankVoucherKind.INTERNAL_TRANSFER, operation_code=None, **base)
    with pytest.raises(ValidationError):
        # TK đích phải khác TK nguồn.
        BankVoucherIn(
            kind=BankVoucherKind.INTERNAL_TRANSFER,
            operation_code=None,
            counter_bank_account_id=1,
            **base,
        )
    with pytest.raises(ValidationError):
        # Ba loại còn lại bắt buộc nghiệp vụ.
        BankVoucherIn(kind=BankVoucherKind.CREDIT_ADVICE, operation_code=None, **base)
    with pytest.raises(ValidationError):
        # Số séc chỉ có trên chứng từ séc.
        BankVoucherIn(
            kind=BankVoucherKind.CREDIT_ADVICE,
            operation_code="thu-khac",
            cheque_no="AC-1",
            cheque_date=JAN_15,
            **base,
        )
    with pytest.raises(ValidationError):
        # Số séc và ngày séc sống chết cùng nhau.
        BankVoucherIn(
            kind=BankVoucherKind.CHEQUE, operation_code="chi-khac", cheque_no="AC-1", **base
        )
