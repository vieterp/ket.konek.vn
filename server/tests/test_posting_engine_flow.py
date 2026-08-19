"""Vòng đời ghi sổ trên PostgreSQL thật — trái tim của phase 4.

Bốn nhóm bất biến, mỗi nhóm là một tiêu chí thành công của phase file:

* **Hai sổ** (N3, FR-NFR-031): một chứng từ sinh dòng ở cả hai sổ; dòng sổ
  quản trị riêng không ảnh hưởng sổ tài chính.
* **Đúng số** (FR-NFR-001/002): quy đổi ngoại tệ đúng `round_money`; chứng từ
  lệch bị từ chối với **toàn bộ** vi phạm một lượt, thông điệp nêu số lệch.
* **post → unpost → post lại giống hệt** — bỏ ghi sổ chỉ đụng `gl_postings`.
* **Kỳ khóa bất khả xâm phạm** ở cả tầng dịch vụ lẫn tầng DB (trigger) — test
  gọi SQL trực tiếp cũng phải lỗi (success criteria phase-04).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    PostingValidationError,
    RowVersionConflictError,
    VoucherTransitionError,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.models import JournalLine
from ket.modules.general_ledger.journal.schemas import (
    ExtendedDimensionIn,
    JournalLineIn,
    JournalVoucherIn,
)
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting, Ledger, PostingDimensionValue
from posting_support import (
    PostingContext,
    posting_scope,
    seed_posting_context,
)

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"
JAN_15 = date(2026, 1, 15)
FEB_10 = date(2026, 2, 10)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    """Chạy một hàm nghiệp vụ trong transaction với scope chi nhánh test."""

    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _vnd_line(account: int, *, debit: int = 0, credit: int = 0) -> JournalLineIn:
    return JournalLineIn(account_id=account, debit_fc=Decimal(debit), credit_fc=Decimal(credit))


def _payload(
    context: PostingContext,
    *,
    posting_date: date = JAN_15,
    lines: tuple[JournalLineIn, ...] | None = None,
    currency: str = VND,
    rate: Decimal = Decimal(1),
) -> JournalVoucherIn:
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency,
        exchange_rate=rate,
        description="chứng từ test",
        lines=lines
        or (
            _vnd_line(context.accounts["642"], debit=100_000),
            _vnd_line(context.accounts["111"], credit=100_000),
        ),
    )


def _postings_of(session: Session, voucher_id: UUID) -> list[GlPosting]:
    return list(
        session.execute(
            select(GlPosting)
            .where(GlPosting.voucher_id == voucher_id)
            .order_by(GlPosting.ledger, GlPosting.line_no)
        )
        .scalars()
        .all()
    )


def test_posting_writes_both_ledgers_and_marks_the_queue(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_CAT
        assert voucher.voucher_no.startswith("GLE")

        service.post(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_GHI_SO
        assert voucher.posted_by == ACTOR_ID

        rows = _postings_of(session, voucher.id)
        assert [row.ledger for row in rows] == [0, 0, 1, 1]
        for row in rows:
            assert row.branch_id == context.branch_id
            assert row.period_id == voucher.period_id
        financial = [row for row in rows if row.ledger == Ledger.FINANCIAL]
        assert sum(row.debit for row in financial) == Decimal("100000.00")
        assert sum(row.credit for row in financial) == Decimal("100000.00")

        marks = (
            session.execute(
                select(BalanceRecalcQueue).where(
                    BalanceRecalcQueue.branch_id == context.branch_id,
                    BalanceRecalcQueue.from_period_id == voucher.period_id,
                )
            )
            .scalars()
            .all()
        )
        assert {mark.ledger for mark in marks} == {0, 1}
        return voucher.id

    run(work)


def test_a_bad_voucher_reports_every_violation_at_once(
    run: Runner, context: PostingContext
) -> None:
    """Lệch Nợ/Có + TK tổng hợp + thiếu khách hàng của TK 131 — một lần ném đủ ba."""

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(
            _payload(
                context,
                lines=(
                    _vnd_line(context.accounts["11"], debit=120_000),
                    _vnd_line(context.accounts["131"], credit=100_000),
                ),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        codes = {violation.code for violation in caught.value.violations}
        assert codes == {
            "posting.unbalanced",
            "posting.unbalanced_fc",
            "account.summary",
            "dimension.missing",
        }
        unbalanced = next(v for v in caught.value.violations if v.code == "posting.unbalanced")
        assert "20000" in str(unbalanced.details["gap"])
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


class _RollbackError(Exception):
    """Chủ động rollback transaction test sau khi đã khẳng định xong."""


def test_multi_currency_voucher_converts_with_round_money(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        rate = Decimal("25000.5")
        voucher = service.create(
            _payload(
                context,
                currency="USD",
                rate=rate,
                lines=(
                    JournalLineIn(
                        account_id=context.accounts["112"],
                        debit_fc=Decimal("100.33"),
                        currency_code="USD",
                        exchange_rate=rate,
                    ),
                    JournalLineIn(
                        account_id=context.accounts["111"],
                        credit_fc=Decimal("100.33"),
                        currency_code="USD",
                        exchange_rate=rate,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        rows = _postings_of(session, voucher.id)
        # 100.33 × 25000.5 = 2 508 300.165 → nửa lên 2 508 300.17 (ROUND_HALF_UP)
        assert rows[0].debit == Decimal("2508300.17")
        assert rows[0].debit_fc == Decimal("100.33")
        assert rows[0].currency_code == "USD"
        assert rows[1].credit == Decimal("2508300.17")

    run(work)


def test_mismatched_client_conversion_is_refused(run: Runner, context: PostingContext) -> None:
    """Validator #9: client tự làm tròn kiểu khác thì bị chặn ngay lúc lưu."""

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        rate = Decimal("25000.5")
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                _payload(
                    context,
                    currency="USD",
                    rate=rate,
                    lines=(
                        JournalLineIn(
                            account_id=context.accounts["112"],
                            debit_fc=Decimal("100.33"),
                            debit=Decimal("2508300.16"),  # nửa chẵn — sai luật
                            currency_code="USD",
                            exchange_rate=rate,
                        ),
                        JournalLineIn(
                            account_id=context.accounts["111"],
                            credit_fc=Decimal("100.33"),
                            currency_code="USD",
                            exchange_rate=rate,
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == "posting.conversion_mismatch"
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_post_unpost_repost_reproduces_identical_rows(run: Runner, context: PostingContext) -> None:
    compare_columns = [column.name for column in GlPosting.__table__.columns if column.name != "id"]

    def snapshot(session: Session, voucher_id: UUID) -> list[tuple[object, ...]]:
        return [
            tuple(getattr(row, name) for name in compare_columns)
            for row in _postings_of(session, voucher_id)
        ]

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        service.post(voucher.id, user_id=ACTOR_ID)
        first = snapshot(session, voucher.id)
        assert first

        service.unpost(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_CAT
        assert voucher.posted_at is None
        assert _postings_of(session, voucher.id) == []
        # Dòng chi tiết của module KHÔNG bị đụng khi bỏ ghi sổ.
        lines = (
            session.execute(select(JournalLine).where(JournalLine.voucher_id == voucher.id))
            .scalars()
            .all()
        )
        assert len(lines) == 2

        service.post(voucher.id, user_id=ACTOR_ID)
        assert snapshot(session, voucher.id) == first

    run(work)


def test_backdated_voucher_marks_the_earlier_period(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        feb = service.create(_payload(context, posting_date=FEB_10), user_id=ACTOR_ID)
        service.post(feb.id, user_id=ACTOR_ID)

        jan = service.create(_payload(context, posting_date=JAN_15), user_id=ACTOR_ID)
        service.post(jan.id, user_id=ACTOR_ID)

        assert jan.period_id != feb.period_id
        jan_marks = (
            session.execute(
                select(BalanceRecalcQueue).where(
                    BalanceRecalcQueue.branch_id == context.branch_id,
                    BalanceRecalcQueue.from_period_id == jan.period_id,
                    BalanceRecalcQueue.ledger == Ledger.FINANCIAL.value,
                )
            )
            .scalars()
            .all()
        )
        assert len(jan_marks) == 1

    run(work)


def test_extended_dimension_required_and_value_checked(
    run: Runner, context: PostingContext
) -> None:
    """Chiều mở rộng `is_required` theo tiền tố TK: thiếu → chặn; giá trị lạ →
    chặn; đủ và đúng → dòng vào `posting_dimension_values`."""
    from ket.kernel.dimensions.service import DimensionService

    def work(session: Session) -> object:
        dimension = DimensionService(session).declare(
            code=f"KV{context.branch_id}",
            name="Khu vực",
            is_required=True,
            applies_to_accounts=["642"],
        )
        value = DimensionService(session).add_value(dimension, code="BAC", name="Miền Bắc")

        service = JournalVoucherService(session)
        bare = service.create(_payload(context), user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError) as caught:
            service.post(bare.id, user_id=ACTOR_ID)
        assert any(v.code == "dimension.extended_missing" for v in caught.value.violations)

        filled = service.create(
            _payload(
                context,
                lines=(
                    JournalLineIn(
                        account_id=context.accounts["642"],
                        debit_fc=Decimal(50_000),
                        extended=(
                            ExtendedDimensionIn(dimension_id=dimension.id, value_id=value.id),
                        ),
                    ),
                    _vnd_line(context.accounts["111"], credit=50_000),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(filled.id, user_id=ACTOR_ID)
        stored = (
            session.execute(
                select(PostingDimensionValue)
                .join(GlPosting, GlPosting.id == PostingDimensionValue.posting_id)
                .where(GlPosting.voucher_id == filled.id)
            )
            .scalars()
            .all()
        )
        assert {(row.dimension_id, row.value_id) for row in stored} == {(dimension.id, value.id)}
        # Hai sổ → hai dòng phát sinh cùng mang chiều.
        assert len(stored) == 2

        ghost = service.create(
            _payload(
                context,
                lines=(
                    JournalLineIn(
                        account_id=context.accounts["642"],
                        debit_fc=Decimal(1_000),
                        extended=(
                            ExtendedDimensionIn(dimension_id=dimension.id, value_id=999_999),
                        ),
                    ),
                    _vnd_line(context.accounts["111"], credit=1_000),
                ),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as unknown:
            service.post(ghost.id, user_id=ACTOR_ID)
        assert any(v.code == "dimension.extended_value_unknown" for v in unknown.value.violations)
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_locked_period_blocks_service_and_database_layers(
    run: Runner,
    context: PostingContext,
    owner_engine: Engine,
    dataset_alpha: DatasetRef,
) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context, posting_date=FEB_10), user_id=ACTOR_ID)
        PeriodService(session).lock_period(voucher.period_id, user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "period.locked"
        # Bỏ ghi sổ trong kỳ khóa cũng bị chặn — dựng bằng chứng từ đã ghi sổ:
        PeriodService(session).unlock_period(voucher.period_id)
        service.post(voucher.id, user_id=ACTOR_ID)
        PeriodService(session).lock_period(voucher.period_id, user_id=ACTOR_ID)
        with pytest.raises(PostingValidationError):
            service.unpost(voucher.id, user_id=ACTOR_ID)
        return {"id": voucher.id, "period_id": voucher.period_id}

    voucher = run(work)

    # Tầng DB: INSERT thẳng bằng SQL (đường đi vòng qua service) phải bị trigger
    # `gl_postings_period_guard` chặn — kể cả khi chạy bằng `ket_owner`.
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(
            f'SET LOCAL search_path TO "{dataset_alpha.schema_name}", public'
        )
        with pytest.raises(DBAPIError, match="PERIOD_LOCKED"):
            connection.execute(
                text(
                    "INSERT INTO gl_postings (voucher_id, line_no, ledger, branch_id,"
                    " posting_date, period_id, account_id, currency_code, exchange_rate,"
                    " debit_fc, credit_fc, debit, credit)"
                    " VALUES (:voucher_id, 99, 0, :branch_id, :posting_date, :period_id,"
                    " :account_id, 'VND', 1, 1, 0, 1, 0)"
                ),
                {
                    "voucher_id": voucher["id"],
                    "branch_id": context.branch_id,
                    "posting_date": FEB_10,
                    "period_id": voucher["period_id"],
                    "account_id": context.accounts["111"],
                },
            )

    # DELETE thẳng cũng bị chặn.
    with owner_engine.begin() as connection:
        connection.exec_driver_sql(
            f'SET LOCAL search_path TO "{dataset_alpha.schema_name}", public'
        )
        with pytest.raises(DBAPIError, match="PERIOD_LOCKED"):
            connection.execute(
                text("DELETE FROM gl_postings WHERE voucher_id = :voucher_id"),
                {"voucher_id": voucher["id"]},
            )

    # Mở lại kỳ để không ảnh hưởng test khác dùng chung năm tài chính.
    def unlock(session: Session) -> None:
        PeriodService(session).unlock_period(voucher["period_id"])
        JournalVoucherService(session).unpost(voucher["id"], user_id=ACTOR_ID)

    run(unlock)


def test_save_also_posts_setting_posts_in_the_same_transaction(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        # Upsert thẳng dòng `settings`: test API cùng nhóm có thể đã tạo dòng
        # này (giá trị "false"), nên `set_setting(expected_row_version=None)`
        # sẽ 409 tùy thứ tự chạy — đường tắt này miễn nhiễm thứ tự.
        from ket.kernel.security.models import Setting

        row = session.scalar(
            select(Setting).where(Setting.key == SAVE_ALSO_POSTS_KEY, Setting.scope == "system")
        )
        if row is None:
            session.add(
                Setting(scope="system", key=SAVE_ALSO_POSTS_KEY, value="true", value_type="boolean")
            )
        else:
            row.value = "true"
        session.flush()
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_GHI_SO
        assert _postings_of(session, voucher.id)
        raise _RollbackError  # không để tùy chọn hệ thống dính sang test khác

    with pytest.raises(_RollbackError):
        run(work)


def test_update_replaces_lines_and_respects_row_version(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        updated_payload = _payload(
            context,
            lines=(
                _vnd_line(context.accounts["642"], debit=70_000),
                _vnd_line(context.accounts["112"], credit=70_000),
            ),
        )
        service.update(
            voucher.id,
            updated_payload,
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )
        lines = (
            session.execute(
                select(JournalLine)
                .where(JournalLine.voucher_id == voucher.id)
                .order_by(JournalLine.line_no)
            )
            .scalars()
            .all()
        )
        assert [line.debit_fc for line in lines] == [Decimal(70_000), Decimal(0)]

        with pytest.raises(RowVersionConflictError):
            service.update(
                voucher.id,
                updated_payload,
                expected_row_version=1_000,
                user_id=ACTOR_ID,
            )

    run(work)


def test_posted_vouchers_refuse_edit_and_delete(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        service.post(voucher.id, user_id=ACTOR_ID)

        with pytest.raises(VoucherTransitionError):
            service.update(
                voucher.id,
                _payload(context),
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        with pytest.raises(VoucherTransitionError):
            service.delete(voucher.id)

        service.unpost(voucher.id, user_id=ACTOR_ID)
        service.delete(voucher.id)
        assert session.get(Voucher, voucher.id) is None
        remaining = (
            session.execute(select(JournalLine).where(JournalLine.voucher_id == voucher.id))
            .scalars()
            .all()
        )
        assert remaining == []

    run(work)


def test_separate_management_lines_do_not_touch_the_financial_ledger(
    run: Runner, context: PostingContext
) -> None:
    """FR-NFR-031: sổ quản trị ghi khác sổ tài chính qua `management_lines`."""
    from ket.modules.general_ledger.journal.service import JOURNAL_NUMBERING_RULE
    from ket.posting.documents.service import VoucherDraft, VoucherService
    from ket.posting.engine.requests import PostingLine, PostingRequest
    from ket.posting.engine.service import PostingService

    def work(session: Session) -> object:
        voucher = VoucherService(session).create(
            VoucherDraft(
                document_type="GLE",
                branch_id=context.branch_id,
                document_date=JAN_15,
                posting_date=JAN_15,
                currency_code=VND,
                exchange_rate=Decimal(1),
            ),
            rule=JOURNAL_NUMBERING_RULE,
            user_id=ACTOR_ID,
        )

        def line(account: str, *, debit: int = 0, credit: int = 0) -> PostingLine:
            return PostingLine(
                account_id=context.accounts[account],
                debit_fc=Decimal(debit),
                credit_fc=Decimal(credit),
                currency=VND,
                rate=Decimal(1),
            )

        PostingService(session).post(
            PostingRequest(
                voucher_id=voucher.id,
                financial_lines=(
                    line("642", debit=10_000),
                    line("111", credit=10_000),
                ),
                management_lines=(
                    line("642", debit=4_000),
                    line("111", credit=4_000),
                ),
            ),
            user_id=ACTOR_ID,
        )
        rows = _postings_of(session, voucher.id)
        financial_total = sum(r.debit for r in rows if r.ledger == Ledger.FINANCIAL)
        management_total = sum(r.debit for r in rows if r.ledger == Ledger.MANAGEMENT)
        assert financial_total == Decimal("10000.00")
        assert management_total == Decimal("4000.00")

    run(work)


def test_voucher_numbers_are_sequential_per_branch(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        first = service.create(_payload(context), user_id=ACTOR_ID)
        second = service.create(_payload(context), user_id=ACTOR_ID)
        first_no = int(first.voucher_no.removeprefix("GLE"))
        second_no = int(second.voucher_no.removeprefix("GLE"))
        assert second_no == first_no + 1

    run(work)


def test_the_posting_transaction_reads_the_period_row_with_for_share() -> None:
    """RT-09, nửa phía ghi sổ — canh bằng SQL biên dịch, không bằng probe khóa.

    Vì sao không đo bằng `SELECT … NOWAIT` từ connection thứ hai: transaction
    ghi sổ còn giữ những khóa **ngầm** trên dòng kỳ (UPDATE `vouchers` mang FK
    tới kỳ), nên probe khóa xanh cả khi `FOR SHARE` đã bị xóa — mutation M2
    của review 4A sống sót qua đúng cách đo đó. Đọc thẳng câu SQL mà
    `PostingService` sẽ chạy là phép canh tất định; test hành vi interleaving
    ghi-sổ ↔ khóa-sổ thật thuộc slice 4D cùng `lock_service` (phase-04 bước 18).
    """
    from sqlalchemy.dialects import postgresql

    from ket.posting.engine.service import period_share_lock_statement

    compiled = str(period_share_lock_statement(1).compile(dialect=postgresql.dialect()))
    assert "FOR SHARE" in compiled
    assert "accounting_periods" in compiled


def test_unpost_marks_the_recalc_queue_again(run: Runner, context: PostingContext) -> None:
    """4B đứng trực tiếp trên dấu bẩn này — bỏ `mark_dirty` ở `unpost` là
    snapshot nói dối sau lần bỏ ghi sổ đầu tiên (mutation M3 của review 4A)."""

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        service.post(voucher.id, user_id=ACTOR_ID)

        session.execute(
            sa_delete(BalanceRecalcQueue).where(
                BalanceRecalcQueue.branch_id == context.branch_id,
                BalanceRecalcQueue.from_period_id == voucher.period_id,
            )
        )
        service.unpost(voucher.id, user_id=ACTOR_ID)
        marks = (
            session.execute(
                select(BalanceRecalcQueue.ledger).where(
                    BalanceRecalcQueue.branch_id == context.branch_id,
                    BalanceRecalcQueue.from_period_id == voucher.period_id,
                )
            )
            .scalars()
            .all()
        )
        assert sorted(marks) == [0, 1]
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_a_closed_fiscal_year_blocks_posting(run: Runner, context: PostingContext) -> None:
    """Quyết toán năm chặn ghi sổ dù kỳ con chưa khóa (mutation M5b review 4A)."""
    from ket.kernel.periods.models import FiscalYear

    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        year = session.get(FiscalYear, context.fiscal_year_id)
        assert year is not None
        year.is_closed = True
        session.flush()
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        assert caught.value.violations[0].code == "period.locked"
        raise _RollbackError  # trả `is_closed` về như cũ cùng rollback

    with pytest.raises(_RollbackError):
        run(work)


def test_changing_the_branch_of_a_saved_voucher_is_refused(
    run: Runner,
    context: PostingContext,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """H1 review 4A: số chứng từ thuộc dãy chi nhánh — PUT không đổi được chi nhánh."""
    from ket.kernel.errors import VoucherBranchImmutableError
    from ket.kernel.organization.service import BranchService
    from ket.kernel.persistence.unit_of_work import RequestScope

    seed_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name, user_id=ACTOR_ID, branch_ids=()
    )
    with unit_of_work(session_factory, seed_scope) as session:
        other = BranchService(session).create(
            code=f"PB2{context.branch_id}", name="Chi nhánh thứ hai"
        )
        other_branch_id = other.id

    wide_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(context.branch_id, other_branch_id),
        acting_branch_id=context.branch_id,
    )
    with unit_of_work(session_factory, wide_scope) as session:
        service = JournalVoucherService(session)
        voucher = service.create(_payload(context), user_id=ACTOR_ID)
        moved = _payload(context).model_copy(update={"branch_id": other_branch_id})
        with pytest.raises(VoucherBranchImmutableError):
            service.update(
                voucher.id,
                moved,
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        service.delete(voucher.id)


def test_an_all_zero_voucher_has_nothing_to_post(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        service = JournalVoucherService(session)
        voucher = service.create(
            _payload(
                context,
                lines=(
                    _vnd_line(context.accounts["642"]),
                    _vnd_line(context.accounts["111"]),
                ),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        assert {v.code for v in caught.value.violations} == {"posting.zero_amount"}
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)
