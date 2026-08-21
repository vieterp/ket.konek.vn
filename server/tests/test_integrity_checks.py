"""Bộ kiểm tra toàn vẹn (lát 4D, bước 15) trên PostgreSQL thật (FR-NFR-007).

Khuôn chung của từng test: dựng dữ liệu ĐÚNG → check xanh; phá đúng một chỗ
bằng đường vòng (SQL/ORM trực tiếp — thứ mà validator lúc ghi sổ không thấy)
→ check chỉ đích danh chỗ phá, và CHỈ chỗ đó. Mỗi test dùng chi nhánh của
module này nên chênh lệch của các tệp test khác không lọt vào (RLS + cột
`branch_id` trong từng câu check).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import IntegrityCheckUnknownError, JobParamsInvalidError
from ket.kernel.jobs.registry import REGISTRY, JobContext
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.master_data.usage import MasterDataUsage
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.balances.models import AccountBalance
from ket.posting.balances.recalc_job import BalanceRecalcParams, run_balance_recalc
from ket.posting.engine.dimensions import PartnerKind
from ket.posting.engine.models import GlPosting
from ket.posting.integrity.checks.registry import CHECKS, check_of
from ket.posting.integrity.job import (
    INTEGRITY_JOB,
    IntegrityCheckParams,
    run_integrity_check,
)
from ket.posting.integrity.runner import resolve_checks, run_check
from ket.posting.opening_balances.models import OpeningBalance, OpeningBalanceInvoice
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_20 = date(2026, 1, 20)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _post_balanced_voucher(session: Session, context: PostingContext) -> UUID:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=JAN_20,
            posting_date=JAN_20,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="chứng từ mẫu integrity",
            lines=(
                JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(80_000)),
                JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(80_000)),
            ),
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


def _totals_by_check(
    session: Session, branch_id: int, codes: tuple[str, ...] | None = None
) -> dict[str, int]:
    checks = resolve_checks(list(codes) if codes else None)
    return {check.code: run_check(session, check, branch_id=branch_id).total for check in checks}


def test_registry_has_exactly_the_declared_checks() -> None:
    assert tuple(check.code for check in CHECKS) == (
        "ledger_balanced",
        "trial_balance_balanced",
        "snapshot_matches_postings",
        "detail_matches_control",
        "opening_balance_balanced",
        "opening_detail_matches_control",
        "usage_counter_accurate",
        "treasurer_book_matches_ledger",
    )
    with pytest.raises(IntegrityCheckUnknownError):
        check_of("khong_ton_tai")


def test_job_type_is_registered_and_requires_a_branch(dataset_alpha: DatasetRef) -> None:
    assert REGISTRY.get("posting.integrity.check") is INTEGRITY_JOB
    context = JobContext(
        job_id=uuid4(),
        session=None,  # type: ignore[arg-type] — bị chặn trước khi chạm session
        progress=FakeProgress(reports=[]),
        attempt=1,
        dataset_schema=dataset_alpha.schema_name,
        branch_id=None,
        requested_by=ACTOR_ID,
    )
    with pytest.raises(JobParamsInvalidError):
        run_integrity_check(context, IntegrityCheckParams())


def test_clean_books_pass_the_branch_scoped_checks(run: Runner, context: PostingContext) -> None:
    """Sổ đúng → mọi check theo-chi-nhánh xanh.

    `usage_counter_accurate` không lọc chi nhánh (bộ đếm danh mục là dữ liệu
    toàn dataset) nên không assert ở đây — test riêng của nó tự dựng và tự dọn.
    """

    def work(session: Session) -> object:
        _post_balanced_voucher(session, context)
        totals = _totals_by_check(
            session,
            context.branch_id,
            (
                "ledger_balanced",
                "trial_balance_balanced",
                "snapshot_matches_postings",
                "detail_matches_control",
                "opening_balance_balanced",
                "opening_detail_matches_control",
            ),
        )
        assert totals == dict.fromkeys(totals, 0), totals
        return None

    run(work)


def test_deleting_one_posting_row_is_caught_by_ledger_balanced(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        good_id = _post_balanced_voucher(session, context)
        bad_id = _post_balanced_voucher(session, context)
        # Phá bằng SQL trực tiếp — đường mà PostingService không bao giờ đi.
        one_row = (
            select(GlPosting.id)
            .where(GlPosting.voucher_id == bad_id)
            .where(GlPosting.ledger == 0)
            .limit(1)
            .scalar_subquery()
        )
        session.execute(sa_delete(GlPosting).where(GlPosting.id == one_row))

        outcome = run_check(session, check_of("ledger_balanced"), branch_id=context.branch_id)
        assert outcome.total == 1
        flagged = outcome.sample[0]
        assert flagged["voucher_id"] == str(bad_id)
        assert flagged["ledger"] == 0
        assert str(good_id) not in {row["voucher_id"] for row in outcome.sample}

        # Dọn: xóa nốt dòng sổ của chứng từ đã phá để các test sau xanh lại.
        session.execute(sa_delete(GlPosting).where(GlPosting.voucher_id == bad_id))
        return None

    run(work)


def test_snapshot_tamper_is_caught_once_the_queue_is_clean(
    run: Runner, context: PostingContext, dataset_alpha: DatasetRef
) -> None:
    """Sau recalc (hàng đợi rỗng) snapshot khớp; sửa tay một dòng snapshot →
    `snapshot_matches_postings` chỉ đúng dòng đó. Khi hàng đợi còn dấu bẩn,
    check im lặng (snapshot được phép cũ)."""

    def work(session: Session) -> object:
        _post_balanced_voucher(session, context)
        # Còn dấu bẩn → check không kêu dù snapshot chưa tính.
        assert (
            run_check(
                session, check_of("snapshot_matches_postings"), branch_id=context.branch_id
            ).total
            == 0
        )

        job_context = JobContext(
            job_id=uuid4(),
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=context.branch_id,
            requested_by=ACTOR_ID,
        )
        run_balance_recalc(job_context, BalanceRecalcParams())
        assert (
            run_check(
                session, check_of("snapshot_matches_postings"), branch_id=context.branch_id
            ).total
            == 0
        )

        row = session.execute(
            select(AccountBalance)
            .where(AccountBalance.branch_id == context.branch_id)
            .where(AccountBalance.period_debit > 0)
            .limit(1)
        ).scalar_one()
        row.period_debit = row.period_debit + Decimal(1)
        session.flush()

        outcome = run_check(
            session, check_of("snapshot_matches_postings"), branch_id=context.branch_id
        )
        assert outcome.total >= 1
        assert any(
            row_out["account_id"] == row.account_id and row_out["period_id"] == row.period_id
            for row_out in outcome.sample
        )

        row.period_debit = row.period_debit - Decimal(1)
        session.flush()
        return None

    run(work)


def test_missing_required_dimension_is_caught_by_detail_matches_control(
    run: Runner, context: PostingContext
) -> None:
    """Kịch bản trôi cấu hình: TK bật `detail_tracking` SAU khi đã có phát sinh
    không có chiều — validator lúc ghi sổ không thể thấy trước điều đó."""

    def work(session: Session) -> object:
        account = ChartOfAccount(
            package_id=context.package_id,
            code=f"642D{context.branch_id}",
            name="Chi phí theo dõi khoản mục (test)",
            path="0.",
            balance_nature=BalanceNature.NONE,
            is_summary=False,
            detail_tracking=None,
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        session.flush()

        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_20,
                posting_date=JAN_20,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="phát sinh trước khi bật tracking",
                lines=(
                    JournalLineIn(account_id=account.id, debit_fc=Decimal(30_000)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(30_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        assert (
            run_check(
                session, check_of("detail_matches_control"), branch_id=context.branch_id
            ).total
            == 0
        )

        account.detail_tracking = ["expense_item"]
        session.flush()

        outcome = run_check(
            session, check_of("detail_matches_control"), branch_id=context.branch_id
        )
        # Hai dòng (sổ tài chính + sổ quản trị) của cùng chứng từ, cùng TK.
        assert outcome.total == 2
        assert {row["voucher_id"] for row in outcome.sample} == {str(voucher.id)}
        assert {row["tracking"] for row in outcome.sample} == {"expense_item"}

        account.detail_tracking = None
        session.flush()
        return None

    run(work)


def test_wrong_partner_kind_is_caught_by_detail_matches_control(
    run: Runner, context: PostingContext
) -> None:
    """Nhánh partner_kind của CASE (review 4D, M-10): TK bật theo dõi KHÁCH
    nhưng dòng mang NCC — "có đối tác" không đủ, phải đúng LOẠI (cùng luật
    `PostingDimensions.has_tracking`)."""

    def work(session: Session) -> object:
        vendor = MasterDataService(session, Partner).create(
            code=f"NCC-INTEG-{context.branch_code}",
            name="NCC test integrity",
            extra={"is_vendor": True},
        )
        account = ChartOfAccount(
            package_id=context.package_id,
            code=f"331D{context.branch_id}",
            name="Phải trả theo dõi khách (test drift)",
            path="0.",
            balance_nature=BalanceNature.DUAL,
            is_summary=False,
            detail_tracking=None,
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        session.flush()

        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_20,
                posting_date=JAN_20,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="dòng mang NCC trước khi TK đổi sang theo dõi khách",
                lines=(
                    JournalLineIn(
                        account_id=account.id,
                        debit_fc=Decimal(20_000),
                        partner_id=vendor.id,
                        partner_kind=PartnerKind.VENDOR,
                    ),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(20_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        # Trôi cấu hình: TK chuyển sang theo dõi KHÁCH — dòng NCC cũ thành lệch.
        account.detail_tracking = ["customer"]
        session.flush()

        outcome = run_check(
            session, check_of("detail_matches_control"), branch_id=context.branch_id
        )
        assert outcome.total == 2  # hai sổ, cùng dòng, cùng TK
        assert {row["voucher_id"] for row in outcome.sample} == {str(voucher.id)}
        assert {row["tracking"] for row in outcome.sample} == {"customer"}

        # Đổi sang đúng loại đang mang → check xanh lại (nhánh vendor của CASE).
        account.detail_tracking = ["vendor"]
        session.flush()
        assert (
            run_check(
                session, check_of("detail_matches_control"), branch_id=context.branch_id
            ).total
            == 0
        )

        account.detail_tracking = None
        session.flush()
        return None

    run(work)


def test_opening_imbalance_and_invoice_drift_are_caught(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        parent = OpeningBalance(
            fiscal_year_id=context.fiscal_year_id,
            ledger=1,
            branch_id=context.branch_id,
            account_id=context.accounts["131"],
            currency_code="VND",
            exchange_rate=Decimal(1),
            debit=Decimal(500_000),
            credit=Decimal(0),
            debit_fc=Decimal(500_000),
            credit_fc=Decimal(0),
            detail_kind=2,
        )
        session.add(parent)
        session.flush()
        session.add(
            OpeningBalanceInvoice(
                opening_balance_id=parent.id,
                invoice_no="HD-001",
                amount_fc=Decimal(400_000),
                amount=Decimal(400_000),
                paid_amount=Decimal(0),
            )
        )
        session.flush()

        imbalance = run_check(
            session, check_of("opening_balance_balanced"), branch_id=context.branch_id
        )
        assert imbalance.total == 1
        assert imbalance.sample[0]["ledger"] == 1
        assert Decimal(str(imbalance.sample[0]["imbalance"])) == Decimal(500_000)

        drift = run_check(
            session, check_of("opening_detail_matches_control"), branch_id=context.branch_id
        )
        assert drift.total == 1
        assert drift.sample[0]["opening_balance_id"] == parent.id
        assert Decimal(str(drift.sample[0]["invoice_total"])) == Decimal(400_000)

        # Xóa SẠCH hóa đơn con: dòng cha nhóm 2/3/4 không còn con vẫn phải bị
        # chỉ mặt (review 4D, M-3 — INNER JOIN cũ mù đúng ca này).
        session.execute(
            sa_delete(OpeningBalanceInvoice).where(
                OpeningBalanceInvoice.opening_balance_id == parent.id
            )
        )
        wiped = run_check(
            session, check_of("opening_detail_matches_control"), branch_id=context.branch_id
        )
        assert wiped.total == 1
        assert Decimal(str(wiped.sample[0]["invoice_total"])) == Decimal(0)

        # Dọn để test khác của module không thấy chênh lệch này nữa.
        session.execute(
            sa_delete(OpeningBalanceInvoice).where(
                OpeningBalanceInvoice.opening_balance_id == parent.id
            )
        )
        session.execute(sa_delete(OpeningBalance).where(OpeningBalance.id == parent.id))
        return None

    run(work)


def test_stray_usage_counter_is_flagged_and_job_reports_it(
    run: Runner, context: PostingContext, dataset_alpha: DatasetRef
) -> None:
    """Phase 4 chưa có người ghi bộ đếm — mọi bộ đếm khác 0 là lệch. Đồng thời
    kiểm job: kết quả mang tổng chênh và từng check."""

    def work(session: Session) -> object:
        marker_id = 987_654_321
        session.add(MasterDataUsage(entity_type="partners", entity_id=marker_id, usage_count=5))
        session.flush()

        outcome = run_check(
            session, check_of("usage_counter_accurate"), branch_id=context.branch_id
        )
        assert any(row["entity_id"] == marker_id for row in outcome.sample)

        job_context = JobContext(
            job_id=uuid4(),
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=context.branch_id,
            requested_by=ACTOR_ID,
        )
        result = run_integrity_check(
            job_context, IntegrityCheckParams(checks=["usage_counter_accurate"])
        )
        assert result is not None
        assert result["total_discrepancies"] >= 1
        checks = result["checks"]
        assert isinstance(checks, list)
        assert checks[0]["code"] == "usage_counter_accurate"
        assert checks[0]["rule"] == "BR-SYS-02"

        session.execute(sa_delete(MasterDataUsage).where(MasterDataUsage.entity_id == marker_id))
        return None

    run(work)


def test_direct_sql_row_wipe_is_visible_to_the_count_and_sample_split(
    run: Runner, context: PostingContext
) -> None:
    """Runner trả TỔNG thật và mẫu bị cắt — phá 3 chứng từ, xin mẫu 2 dòng."""

    def work(session: Session) -> object:
        bad_ids = [str(_post_balanced_voucher(session, context)) for _ in range(3)]
        for voucher_id in bad_ids:
            one_row = (
                select(GlPosting.id)
                .where(GlPosting.voucher_id == UUID(voucher_id))
                .where(GlPosting.ledger == 1)
                .limit(1)
                .scalar_subquery()
            )
            session.execute(sa_delete(GlPosting).where(GlPosting.id == one_row))

        outcome = run_check(
            session,
            check_of("ledger_balanced"),
            branch_id=context.branch_id,
            sample_limit=2,
        )
        assert outcome.total == 3
        assert len(outcome.sample) == 2

        for voucher_id in bad_ids:
            session.execute(sa_delete(GlPosting).where(GlPosting.voucher_id == UUID(voucher_id)))
        return None

    run(work)
