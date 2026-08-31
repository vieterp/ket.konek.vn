"""Chiều `bank_account` trên dòng sổ + chiều chi nhánh của sao kê (lát 6G-1).

Hai món nợ hạ tầng của phase 6, kiểm ở đây vì cả hai chỉ chứng minh được bằng
dữ liệu thật trong PostgreSQL:

* **H-3** — trước lát này "dòng 112x thuộc tài khoản ngân hàng nào" được SUY từ
  thân `bank_vouchers` lúc đọc, nên phiếu quỹ nộp/rút tiền mặt ↔ ngân hàng và
  bút toán tổng hợp gõ thẳng 112x biến mất khỏi mọi báo cáo tiền gửi. Nay câu
  trả lời được GHI vào `gl_postings.bank_account_id`, và 112x bật
  `detail_tracking = bank_account` nên không ai bỏ trống được.
* **H-1** — `bank_statements`/`bank_statement_lines` không có `branch_id` nào,
  nên `test_rls_policy_coverage` (quét theo cột `branch_id`) không nhìn thấy
  chúng và hai bảng đứng ngoài cô lập chi nhánh.

Dataset RIÊNG chứ không `dataset_alpha`: bài kiểm phải BẬT `bank_account` trên
TK 112 của gói cấu hình, và gói của `dataset_alpha` dùng chung với hàng chục
tệp test đang ghi sổ 112 mà không khai tài khoản ngân hàng nào (bẫy nhiễu
dataset dùng chung, doctrine 6B).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from ket.kernel.config.accounts_models import ChartOfAccount, DetailTracking
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.bank.balance_service import deposit_balances_as_of
from ket.modules.bank.models import (
    BankStatement,
    BankStatementLine,
    BankVoucherKind,
    StatementMatchKind,
)
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.bank.statement_branch import sync_statement_branch
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.posting.engine.models import GlPosting, Ledger
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)
_DATASET_CODE = "bankdim6g"

Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture(scope="module")
def dimension_dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(
        owner_engine, code=_DATASET_CODE, name="Chiều TK ngân hàng 6G", scheme="TT99"
    )
    yield ref
    drop_dataset_schema(owner_engine, _DATASET_CODE)


@pytest.fixture(scope="module")
def context(
    session_factory: sessionmaker[Session], dimension_dataset: DatasetRef
) -> PostingContext:
    return seed_posting_context(session_factory, dimension_dataset)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session],
    dimension_dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    seeded = seed_cash_book_package_data(session_factory, dimension_dataset, context)
    seed_bank_package_data(session_factory, dimension_dataset, context)
    scope = posting_scope(dimension_dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        # Đúng thứ gói builtin tt99/tt133 khai cho 112x — gói test tối giản của
        # `posting_support` không mang `detail_tracking` nào.
        session.execute(
            update(ChartOfAccount)
            .where(ChartOfAccount.id == seeded["112"])
            .values(detail_tracking=[DetailTracking.BANK_ACCOUNT])
        )
    return seeded


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dimension_dataset: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dimension_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


@pytest.fixture(scope="module")
def other_context(
    session_factory: sessionmaker[Session], dimension_dataset: DatasetRef
) -> PostingContext:
    """Chi nhánh thứ hai trong CÙNG dataset — đối trọng của mọi phép kiểm RLS."""
    return seed_posting_context(session_factory, dimension_dataset)


@pytest.fixture(scope="module")
def vcb(
    session_factory: sessionmaker[Session],
    dimension_dataset: DatasetRef,
    context: PostingContext,
) -> int:
    return ensure_company_bank_account(session_factory, dimension_dataset, context, code="6G-VCB")


@pytest.fixture(scope="module")
def acb(
    session_factory: sessionmaker[Session],
    dimension_dataset: DatasetRef,
    context: PostingContext,
) -> int:
    return ensure_company_bank_account(session_factory, dimension_dataset, context, code="6G-ACB")


def _withdrawal(
    context: PostingContext, accounts: dict[str, int], *, bank_account_id: int | None
) -> CashVoucherIn:
    """Phiếu thu "rút tiền gửi ngân hàng nhập quỹ" — Nợ 111 / Có 112.

    Đây là hình dạng mà luật quy chủ cũ KHÔNG suy được: không có
    `bank_vouchers` để bám, mà nghiệp vụ thì hằng tuần.
    """
    return CashVoucherIn(
        kind=CashVoucherKind.RECEIPT,
        operation_code="thu-khac",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="VND",
        exchange_rate=Decimal(1),
        payer_receiver_name="Rút tiền gửi",
        description="rút TGNH nhập quỹ",
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["112"],
                amount_fc=Decimal(700_000),
                bank_account_id=bank_account_id,
            ),
        ),
    )


class TestRequiredOn112:
    def test_a_cash_voucher_touching_112_cannot_post_without_a_bank_account(
        self, run: Runner, context: PostingContext, accounts: dict[str, int]
    ) -> None:
        """Cùng luật đã áp cho 131/331 với đối tác: TK bật theo dõi chi tiết thì
        dòng phải điền chiều, và cổng là validator ghi sổ chứ không phải form."""

        def work(session: Session) -> object:
            service = CashVoucherService(session)
            voucher = service.create(
                _withdrawal(context, accounts, bank_account_id=None), user_id=ACTOR_ID
            )
            with pytest.raises(PostingValidationError) as raised:
                service.post(voucher.id, user_id=ACTOR_ID)
            codes = {violation.code for violation in raised.value.violations}
            assert "dimension.missing" in codes, raised.value.violations
            trackings = {violation.details.get("tracking") for violation in raised.value.violations}
            assert DetailTracking.BANK_ACCOUNT in trackings
            return None

        run(work)

    def test_the_cash_side_is_untouched_by_the_new_dimension(
        self, run: Runner, context: PostingContext, accounts: dict[str, int], vcb: int
    ) -> None:
        """Chiều `bank_account` gắn vào bên 112, KHÔNG đổ sang bên 111.

        Bên quỹ vốn không nhận chiều nào (`_EMPTY_DIMENSIONS`); một chiều rò
        sang đó sẽ làm sổ chi tiết tiền gửi đếm cả tiền mặt.
        """

        def work(session: Session) -> object:
            service = CashVoucherService(session)
            voucher = service.create(
                _withdrawal(context, accounts, bank_account_id=vcb), user_id=ACTOR_ID
            )
            service.post(voucher.id, user_id=ACTOR_ID)
            rows = session.execute(
                select(GlPosting.account_id, GlPosting.bank_account_id).where(
                    GlPosting.voucher_id == voucher.id, GlPosting.ledger == Ledger.FINANCIAL
                )
            ).all()
            owners = {row.account_id: row.bank_account_id for row in rows}
            assert owners[accounts["112"]] == vcb
            assert owners[accounts["111"]] is None
            return None

        run(work)


class TestDepositBalanceSeesEveryDocument:
    def test_a_cash_withdrawal_moves_the_bank_account_balance(
        self,
        run: Runner,
        context: PostingContext,
        accounts: dict[str, int],
        vcb: int,
    ) -> None:
        """Review 6E-1 H-3: trước lát này phiếu quỹ chạm 112 rơi vào nhóm
        "(chưa gắn)" và S08-DN lệch sao kê đúng bằng nó."""

        def work(session: Session) -> object:
            before = _balance_of(session, context, vcb)
            service = CashVoucherService(session)
            voucher = service.create(
                _withdrawal(context, accounts, bank_account_id=vcb), user_id=ACTOR_ID
            )
            service.post(voucher.id, user_id=ACTOR_ID)
            after = _balance_of(session, context, vcb)
            # Có 112: tiền RA khỏi tài khoản ngân hàng.
            assert after - before == Decimal(-700_000)
            return None

        run(work)

    def test_an_internal_transfer_splits_by_line_direction(
        self,
        run: Runner,
        context: PostingContext,
        accounts: dict[str, int],
        vcb: int,
        acb: int,
    ) -> None:
        """Luật CTNB giữ nguyên nghĩa sau khi chuyển sang đường ghi: bên **Nợ**
        thuộc tài khoản ĐÍCH, bên **Có** thuộc tài khoản nguồn — một chứng từ
        đứng trên sổ của cả hai tài khoản, mỗi bên một chiều."""

        def work(session: Session) -> object:
            source_before = _balance_of(session, context, vcb)
            target_before = _balance_of(session, context, acb)
            service = BankVoucherService(session)
            voucher = service.create(
                BankVoucherIn(
                    kind=BankVoucherKind.INTERNAL_TRANSFER,
                    operation_code=None,
                    bank_account_id=vcb,
                    counter_bank_account_id=acb,
                    branch_id=context.branch_id,
                    document_date=JAN_15,
                    posting_date=JAN_15,
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    description="chuyển VCB → ACB",
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
            service.post(voucher.id, user_id=ACTOR_ID)
            assert _balance_of(session, context, vcb) - source_before == Decimal(-250_000)
            assert _balance_of(session, context, acb) - target_before == Decimal(250_000)
            return None

        run(work)


def _balance_of(session: Session, context: PostingContext, bank_account_id: int) -> Decimal:
    balances = deposit_balances_as_of(
        session,
        ledger=Ledger.FINANCIAL,
        branch_ids=(context.branch_id,),
        as_of=JAN_15,
    )
    for balance in balances:
        if balance.bank_account_id == bank_account_id:
            return balance.balance
    return Decimal(0)


class TestStatementBranchFollowsTheAccount:
    """Review 6E-1 H-1 — hai bảng sao kê nay có chiều chi nhánh và RLS.

    Chiều lấy từ CHÍNH tài khoản ngân hàng, không từ chi nhánh người nhập:
    `NULL` = tài khoản dùng chung toàn công ty (mọi chi nhánh thấy), một số =
    tài khoản của riêng chi nhánh đó.
    """

    def test_a_company_wide_account_stays_visible_to_every_branch(
        self,
        session_factory: sessionmaker[Session],
        dimension_dataset: DatasetRef,
        context: PostingContext,
        other_context: PostingContext,
        vcb: int,
    ) -> None:
        """`allow_null_branch=True` là quyết định, không phải sơ suất: policy
        chặt hơn sẽ giấu sao kê của tài khoản dùng chung khỏi MỌI người."""
        statement_id = _seed_statement(session_factory, dimension_dataset, context, vcb)
        assert _statement_visible(session_factory, dimension_dataset, context, statement_id)
        assert _statement_visible(session_factory, dimension_dataset, other_context, statement_id)

    def test_a_branch_owned_account_hides_its_statement_from_other_branches(
        self,
        session_factory: sessionmaker[Session],
        dimension_dataset: DatasetRef,
        context: PostingContext,
        other_context: PostingContext,
    ) -> None:
        """Trước lát 6G-1 hai bảng này không có cột `branch_id` nào, nên
        `test_rls_policy_coverage` không nhìn thấy chúng và chúng đứng NGOÀI cô
        lập chi nhánh — dòng sao kê của một chi nhánh đọc được từ chi nhánh
        khác mà không cổng nào chặn."""
        owned = ensure_company_bank_account(
            session_factory,
            dimension_dataset,
            context,
            code="6G-RIENG",
            branch_id=context.branch_id,
        )
        statement_id = _seed_statement(session_factory, dimension_dataset, context, owned)
        assert _statement_visible(session_factory, dimension_dataset, context, statement_id)
        assert not _statement_visible(
            session_factory, dimension_dataset, other_context, statement_id
        )

    def test_lines_carry_the_same_branch_as_their_statement(
        self,
        session_factory: sessionmaker[Session],
        dimension_dataset: DatasetRef,
        context: PostingContext,
        vcb: int,
    ) -> None:
        """Cột trên dòng là bản sao, và RLS lọc theo cột CỦA CHÍNH bảng — một
        dòng lệch chi nhánh với sao kê cha là một dòng lọt/mất đơn lẻ."""
        statement_id = _seed_statement(session_factory, dimension_dataset, context, vcb)
        scope = posting_scope(dimension_dataset, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            statement = session.get(BankStatement, statement_id)
            assert statement is not None
            branches = set(
                session.scalars(
                    select(BankStatementLine.branch_id).where(
                        BankStatementLine.statement_id == statement_id
                    )
                ).all()
            )
            assert branches == {statement.branch_id}


def _seed_statement(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    bank_account_id: int,
) -> UUID:
    """Sao kê một dòng, ghi qua CHÍNH đường đồng bộ chi nhánh dùng chung."""
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        statement = BankStatement(
            bank_account_id=bank_account_id,
            statement_date=JAN_15,
            imported_by=ACTOR_ID,
        )
        session.add(statement)
        session.flush()
        session.add(
            BankStatementLine(
                statement_id=statement.id,
                bank_account_id=bank_account_id,
                line_no=1,
                txn_date=JAN_15,
                credit=Decimal(100_000),
                match_kind=StatementMatchKind.UNMATCHED,
            )
        )
        session.flush()
        sync_statement_branch(session, bank_account_id=bank_account_id)
        return statement.id


def _statement_visible(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    statement_id: UUID,
) -> bool:
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        found = session.scalar(select(BankStatement.id).where(BankStatement.id == statement_id))
        return found is not None
