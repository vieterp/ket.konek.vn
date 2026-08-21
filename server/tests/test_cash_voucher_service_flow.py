"""Vòng đời phiếu thu/chi tiền mặt trên PostgreSQL thật (lát 6B).

Kiểm những bất biến RIÊNG của module quỹ — luật ghi sổ chung (hai sổ, kỳ khóa,
quy đổi) đã có `test_posting_engine_flow.py`:

* Cất cấp số `PT{YY}-`/`PC{YY}-` theo loại; nghiệp vụ phải thuộc gói hiệu lực
  (FR-SYS-025), nghiệp vụ đòi đối tác thì phiếu phải có đối tác đúng loại.
* Mapper trải cặp Nợ/Có thành hai dòng một-bên, chiều gắn bên nghiệp vụ.
* Bộ đếm `master_data_usage` nhích/lùi theo tạo–sửa–xóa (BR-SYS-02, nợ 6A).
* Loại phiếu và chi nhánh bất biến khi sửa.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
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
    return seed_cash_book_package_data(session_factory, dataset_alpha, context)


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


def _receipt(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    operation: str = "thu-khac",
    amount: int = 500_000,
    partner: tuple[PartnerKind, int] | None = None,
    line_partner: tuple[PartnerKind, int] | None = None,
) -> CashVoucherIn:
    partner_kind, partner_id = partner if partner else (None, None)
    lp_kind, lp_id = line_partner if line_partner else (None, None)
    return CashVoucherIn(
        kind=CashVoucherKind.RECEIPT,
        operation_code=operation,
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="VND",
        exchange_rate=Decimal(1),
        partner_kind=partner_kind,
        partner_id=partner_id,
        payer_receiver_name="Nguyễn Văn A",
        description="thu tiền test",
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["131"],
                amount_fc=Decimal(amount),
                partner_kind=lp_kind,
                partner_id=lp_id,
            ),
        ),
    )


def test_create_numbers_by_kind_and_writes_body(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashVoucherService(session)
        receipt = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
        assert receipt.voucher_no.startswith("PT26-")
        assert receipt.document_type == "PT"

        payment = service.create(
            CashVoucherIn(
                kind=CashVoucherKind.PAYMENT,
                operation_code="chi-khac",
                cash_account_id=accounts["111"],
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                lines=(
                    CashVoucherLineIn(
                        debit_account_id=accounts["642"],
                        credit_account_id=accounts["111"],
                        amount_fc=Decimal(120_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        assert payment.voucher_no.startswith("PC26-")
        assert payment.document_type == "PC"

        _, body, lines, settlements = service.get(receipt.id)
        assert body.kind == CashVoucherKind.RECEIPT
        assert body.operation_code == "thu-khac"
        assert len(lines) == 1 and settlements == []
        return None

    run(work)


def test_an_operation_outside_the_active_package_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(
                _receipt(context, accounts, operation="nghiep-vu-la"), user_id=ACTOR_ID
            )
        assert caught.value.violations[0].code == "cash.operation_unknown"
        return None

    run(work)


def test_an_operation_requiring_a_partner_demands_the_right_kind(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashVoucherService(session)
        with pytest.raises(PostingValidationError) as missing:
            service.create(
                _receipt(context, accounts, operation="thu-no-khach-hang"), user_id=ACTOR_ID
            )
        assert missing.value.violations[0].code == "cash.operation_partner_required"

        with pytest.raises(PostingValidationError) as wrong_kind:
            service.create(
                _receipt(
                    context,
                    accounts,
                    operation="thu-no-khach-hang",
                    partner=(PartnerKind.VENDOR, 9),
                ),
                user_id=ACTOR_ID,
            )
        assert wrong_kind.value.violations[0].code == "cash.operation_partner_required"
        return None

    run(work)


def test_posting_splits_pairs_and_puts_dimensions_on_the_business_side(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Một cặp Nợ 111/Có 131 thành hai dòng một-bên; đối tác chỉ nằm trên dòng
    131 — thẻ công nợ phase 7 đọc `gl_postings` không được đếm đôi."""

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                operation="thu-no-khach-hang",
                partner=(PartnerKind.CUSTOMER, 77),
                line_partner=(PartnerKind.CUSTOMER, 77),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        rows = (
            session.execute(
                select(GlPosting)
                .where(GlPosting.voucher_id == voucher.id)
                .where(GlPosting.ledger == Ledger.FINANCIAL.value)
                .order_by(GlPosting.line_no)
            )
            .scalars()
            .all()
        )
        assert [row.account_id for row in rows] == [accounts["111"], accounts["131"]]
        cash_row, receivable_row = rows
        assert cash_row.debit == Decimal(500_000) and cash_row.partner_id is None
        assert receivable_row.credit == Decimal(500_000)
        assert receivable_row.partner_id == 77
        assert receivable_row.corresponding_account_id == accounts["111"]
        return None

    run(work)


def test_a_draft_line_missing_one_side_cannot_be_posted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashVoucherService(session)
        payload = _receipt(context, accounts)
        payload = payload.model_copy(
            update={
                "lines": (
                    CashVoucherLineIn(
                        debit_account_id=accounts["111"],
                        credit_account_id=None,
                        amount_fc=Decimal(10_000),
                    ),
                )
            }
        )
        voucher = CashVoucherService(session).create(payload, user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "cash.line_side_missing"
        return None

    run(work)


def test_usage_counters_follow_create_update_delete(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Đối tác trên phiếu + trên dòng nhích bộ đếm; sửa đổi đối tác chuyển bộ
    đếm; xóa trả về đúng số ban đầu (BR-SYS-02, nợ 6A)."""

    def work(session: Session) -> object:
        partner_a, partner_b = 5011, 5012
        before_a = usage_count_of(session, entity_type="partners", entity_id=partner_a)
        before_b = usage_count_of(session, entity_type="partners", entity_id=partner_b)

        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                operation="thu-no-khach-hang",
                partner=(PartnerKind.CUSTOMER, partner_a),
                line_partner=(PartnerKind.CUSTOMER, partner_a),
            ),
            user_id=ACTOR_ID,
        )
        assert usage_count_of(session, entity_type="partners", entity_id=partner_a) == before_a + 2

        service.update(
            voucher.id,
            _receipt(
                context,
                accounts,
                operation="thu-no-khach-hang",
                partner=(PartnerKind.CUSTOMER, partner_b),
                line_partner=(PartnerKind.CUSTOMER, partner_b),
            ),
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )
        assert usage_count_of(session, entity_type="partners", entity_id=partner_a) == before_a
        assert usage_count_of(session, entity_type="partners", entity_id=partner_b) == before_b + 2

        service.delete(voucher.id)
        assert usage_count_of(session, entity_type="partners", entity_id=partner_b) == before_b
        return None

    run(work)


def test_kind_is_immutable_on_update(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(_receipt(context, accounts), user_id=ACTOR_ID)
        flipped = _receipt(context, accounts).model_copy(
            update={
                "kind": CashVoucherKind.PAYMENT,
                "operation_code": "chi-khac",
            }
        )
        with pytest.raises(PostingValidationError) as caught:
            service.update(
                voucher.id,
                flipped,
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "cash.kind_immutable"
        return None

    run(work)


def test_client_amount_echo_is_checked_against_round_money(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        payload = _receipt(context, accounts).model_copy(
            update={
                "currency_code": "USD",
                "exchange_rate": Decimal(25_000),
                "lines": (
                    CashVoucherLineIn(
                        debit_account_id=accounts["111"],
                        credit_account_id=accounts["131"],
                        amount_fc=Decimal(10),
                        amount=Decimal(999),
                    ),
                ),
            }
        )
        with pytest.raises(PostingValidationError) as caught:
            CashVoucherService(session).create(payload, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "posting.conversion_mismatch"
        return None

    run(work)
