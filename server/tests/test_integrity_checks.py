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
from importlib import resources
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import IntegrityCheckUnknownError, JobParamsInvalidError
from ket.kernel.jobs.registry import REGISTRY, JobContext
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.master_data.usage import MasterDataUsage
from ket.kernel.periods.models import (
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
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
        "usage_counter_accurate",
        "treasurer_book_matches_ledger",
        "settlement_matches_subledger",
        "arap_matches_control",
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
        invoice = OpeningBalanceInvoice(
            opening_balance_id=parent.id,
            branch_id=parent.branch_id,
            invoice_no="HD-001",
            amount_fc=Decimal(400_000),
            amount=Decimal(400_000),
            paid_amount=Decimal(0),
        )
        session.add(invoice)
        session.flush()
        invoice_id = invoice.id

        def detail_drift() -> list[dict[str, object]]:
            """Câu `opening_detail_matches_control` — nạp THẲNG từ gói.

            Nó bị GỠ khỏi `CHECKS` ở lát 7C-5 (đỏ trên dữ liệu đúng từ năm thứ
            hai trở đi; đầu tệp `.sql` ghi đủ lý do và điều kiện đăng ký lại),
            nên `check_of` sẽ ném. Bài vẫn giữ để lần đăng ký lại có chỗ bắt
            đầu: nó canh đúng phần câu vẫn đúng — dòng cha do NHẬP sinh ra.
            """
            rows = session.execute(
                text(
                    resources.files("ket.posting.integrity.checks")
                    .joinpath("opening_detail_matches_control.sql")
                    .read_text("utf-8")
                ),
                {"branch_id": context.branch_id},
            ).mappings()
            return [dict(row) for row in rows]

        imbalance = run_check(
            session, check_of("opening_balance_balanced"), branch_id=context.branch_id
        )
        assert imbalance.total == 1
        assert imbalance.sample[0]["ledger"] == 1
        assert Decimal(str(imbalance.sample[0]["imbalance"])) == Decimal(500_000)

        # Chi tiết 400.000 nằm TRONG dòng cha 500.000 — hợp lệ từ lát 7C-5:
        # phần dư không gắn chứng từ đầu kỳ nào thuộc các nguồn công nợ khác
        # (`ar_ap_ledger`), thứ mà `arap_matches_control` mới đo được.
        assert detail_drift() == []

        # Chi tiết VƯỢT dòng cha thì không nguồn nào giải thích được.
        overrun = session.get(OpeningBalanceInvoice, invoice_id)
        assert overrun is not None
        overrun.amount = Decimal(600_000)
        overrun.amount_fc = Decimal(600_000)
        session.flush()
        (drift,) = detail_drift()
        assert drift["opening_balance_id"] == parent.id
        assert Decimal(str(drift["invoice_total"])) == Decimal(600_000)

        # Dòng cha LƯỠNG TÍNH (BR-OPB-03): dư `Nợ 400.000 / Có 100.000` với
        # một chứng từ 400.000 và một khoản ứng trước 100.000 — tổng có dấu
        # 300.000 bằng đúng dư ròng. Bản trước lát 7C-5 so 400.000 với
        # `debit + credit = 500.000` nên nó ĐỎ ở đây, trên dữ liệu mà chính
        # lượt nhập 4C sinh ra (điều kiện #7).
        overrun.amount = Decimal(400_000)
        overrun.amount_fc = Decimal(400_000)
        parent.debit = Decimal(400_000)
        parent.debit_fc = Decimal(400_000)
        parent.credit = Decimal(100_000)
        parent.credit_fc = Decimal(100_000)
        session.add(
            OpeningBalanceInvoice(
                opening_balance_id=parent.id,
                branch_id=parent.branch_id,
                amount_fc=Decimal(100_000),
                amount=Decimal(100_000),
                is_advance=True,
            )
        )
        session.flush()
        assert detail_drift() == []

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


def test_arap_control_matches_the_subledger_on_valid_data(
    run: Runner, context: PostingContext
) -> None:
    """`arap_matches_control` (BR-GLE-05) — đăng ký ở lát 7C-5 sau năm lát.

    Đo trên một TK công nợ RIÊNG của bài: `control` chỉ nhìn TK có
    `detail_tracking`, nên một TK mới dựng ở đây cho phép khẳng định "0 dòng
    lệch cho ĐÚNG tổ hợp này" mà không phụ thuộc dư lượng của tệp test khác —
    lần thứ năm của bài học "bài test phụ thuộc thứ tự tệp".

    Dữ liệu ĐÚNG gồm cả ba nguồn công nợ mà chín điều kiện nói tới: dòng cha
    lưỡng tính đầu kỳ (#7), khoản nợ ghi thẳng bằng chứng từ nghiệp vụ khác
    (#1), và một lượt đối trừ (#2).
    """

    def work(session: Session) -> object:
        account = ChartOfAccount(
            package_id=context.package_id,
            code="1319",
            name="Phải thu khách hàng (bài kiểm toàn vẹn)",
            path="0.",
            balance_nature=BalanceNature.DEBIT,
            is_summary=False,
            detail_tracking=["customer"],
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        customer = MasterDataService(session, Partner).create(
            code=f"KH-ARAP-{context.branch_code}",
            name="Khách kiểm toàn vẹn",
            extra={"is_customer": True},
        )
        session.flush()

        def rows_for_account() -> list[dict[str, object]]:
            """Chạy thẳng câu SQL thay vì lọc `outcome.sample`.

            `run_check` lấy mẫu `LIMIT 50` KHÔNG kèm `ORDER BY`, nên khẳng định
            "không thấy trong mẫu" chỉ nói được "không thấy trong 50 dòng bất
            kỳ" — đúng loại bẫy mà bài này đang muốn tránh.
            """
            rows = session.execute(
                text(check_of("arap_matches_control").sql()),
                {"branch_id": context.branch_id},
            ).mappings()
            return [dict(row) for row in rows if row["account_id"] == account.id]

        # Dòng cha lưỡng tính: dư `Nợ 1.000.000 / Có 300.000`, hai dòng con
        # ngược chiều nhau. Trước lát 7C-5 khoản ứng trước không có dòng con
        # nào và câu này trả `difference = -300.000` trên dữ liệu hợp lệ.
        parent = OpeningBalance(
            fiscal_year_id=context.fiscal_year_id,
            ledger=0,
            branch_id=context.branch_id,
            account_id=account.id,
            currency_code="VND",
            exchange_rate=Decimal(1),
            partner_id=customer.id,
            partner_kind=PartnerKind.CUSTOMER.value,
            detail_kind=2,
            debit=Decimal(1_000_000),
            debit_fc=Decimal(1_000_000),
            credit=Decimal(300_000),
            credit_fc=Decimal(300_000),
        )
        session.add(parent)
        session.flush()
        session.add_all(
            [
                OpeningBalanceInvoice(
                    opening_balance_id=parent.id,
                    branch_id=parent.branch_id,
                    invoice_no="HD-ARAP",
                    invoice_date=date(2025, 12, 1),
                    amount=Decimal(1_000_000),
                    amount_fc=Decimal(1_000_000),
                ),
                OpeningBalanceInvoice(
                    opening_balance_id=parent.id,
                    branch_id=parent.branch_id,
                    amount=Decimal(300_000),
                    amount_fc=Decimal(300_000),
                    is_advance=True,
                ),
            ]
        )
        session.flush()
        assert rows_for_account() == []

        # Khoản nợ ghi thẳng bằng chứng từ nghiệp vụ khác: sổ cái nhích 250.000
        # và sổ phụ phải nhích đúng bằng ấy (điều kiện #1, đóng ở 7C-3).
        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_20,
                posting_date=JAN_20,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="ghi nợ khách bằng chứng từ nghiệp vụ khác",
                lines=(
                    JournalLineIn(
                        account_id=account.id,
                        debit_fc=Decimal(250_000),
                        partner_id=customer.id,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(250_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        assert rows_for_account() == []

        # Tiêm lại lỗi: xóa dòng sổ phụ của chứng từ ấy bằng SQL. Vế sổ cái
        # đứng yên, vế sổ phụ tụt 250.000 — check phải chỉ mặt đúng một dòng.
        session.execute(
            text("DELETE FROM ar_ap_ledger WHERE document_id = :document_id"),
            {"document_id": voucher.id},
        )
        (broken,) = rows_for_account()
        assert Decimal(str(broken["difference"])) == Decimal(250_000)
        assert broken["partner_id"] == customer.id

        # Dọn sạch để tệp khác không thấy TK công nợ này.
        session.execute(
            sa_delete(OpeningBalanceInvoice).where(
                OpeningBalanceInvoice.opening_balance_id == parent.id
            )
        )
        session.execute(sa_delete(OpeningBalance).where(OpeningBalance.id == parent.id))
        session.execute(sa_delete(GlPosting).where(GlPosting.voucher_id == voucher.id))
        account.detail_tracking = None
        session.flush()
        return None

    run(work)


def test_arap_control_goes_quiet_while_the_carry_forward_is_pending(
    run: Runner, context: PostingContext
) -> None:
    """Điều kiện #10: cửa sổ giữa "mở năm mới" và "chạy chuyển số dư".

    Trong cửa sổ ấy vế sổ cái chỉ có phát sinh của năm mới còn vế sổ phụ mang
    mọi khoản treo từ trước (nhánh `ar_ap_ledger` cố ý không lọc năm, #4), nên
    câu biến MỌI khoản công nợ đang treo thành một dòng đỏ. Nó phải tắt hẳn
    thay vì kêu.

    Bài dựng đúng dữ liệu làm câu ĐỎ nếu thiếu cổng — một khoản nợ ghi thẳng
    còn treo — rồi hỏi câu ở một năm tài chính đã mở mà chưa có dòng số dư nào.
    Không có bước dựng ấy thì bài xanh vì `control` rỗng, tức xanh mà không
    chứng minh gì.
    """

    def work(session: Session) -> object:
        account = ChartOfAccount(
            package_id=context.package_id,
            code="1318",
            name="Phải thu khách hàng (bài cửa sổ chuyển năm)",
            path="0.",
            balance_nature=BalanceNature.DEBIT,
            is_summary=False,
            detail_tracking=["customer"],
        )
        session.add(account)
        session.flush()
        account.path = f"{account.id}."
        customer = MasterDataService(session, Partner).create(
            code=f"KH-PENDING-{context.branch_code}",
            name="Khách cửa sổ chuyển năm",
            extra={"is_customer": True},
        )
        service = JournalVoucherService(session)
        voucher = service.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=JAN_20,
                posting_date=JAN_20,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="khoản nợ còn treo sang năm sau",
                lines=(
                    JournalLineIn(
                        account_id=account.id,
                        debit_fc=Decimal(250_000),
                        partner_id=customer.id,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(250_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        # Năm 2060, cách xa mọi năm mà tệp test khác dựng (2025–2027, 2088–2089,
        # 2098): `_target_year_of` tìm năm nhận theo NGÀY LIỀN SAU, nên một năm
        # mới đứng cạnh năm của tệp khác sẽ lặng lẽ thành "năm liền sau" của nó
        # và làm đỏ một bài không liên quan — lần thứ năm của bẫy phụ thuộc thứ
        # tự tệp trong dự án này.
        existing = session.scalar(select(FiscalYear.id).where(FiscalYear.code == "2060"))
        pending = (
            existing
            if existing is not None
            else PeriodService(session)
            .create_fiscal_year(
                code="2060",
                start_date=date(2060, 1, 1),
                accounting_scheme=AccountingScheme.TT99,
                base_currency="VND",
                inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
                vat_method=VatMethod.DEDUCTION,
            )
            .id
        )

        def rows_at(as_of: str) -> list[dict[str, object]]:
            """Chạy câu với `CURRENT_DATE` thay bằng một mốc ngày cho trước.

            `CURRENT_DATE` của phiên không đổi được trong một test, nên thay
            đúng một token — phần còn lại của câu, gồm cả vế cổng #10, giữ
            nguyên như bản chạy thật.
            """
            rows = session.execute(
                text(check_of("arap_matches_control").sql().replace("CURRENT_DATE", as_of)),
                {"branch_id": context.branch_id},
            ).mappings()
            return [dict(row) for row in rows if row["account_id"] == account.id]

        # Năm 2060 đã mở, chưa có dòng số dư nào, và có năm trước ⇒ câu tắt.
        assert rows_at("DATE '2060-06-30'") == []

        # Đối chứng 1: cùng câu, cùng dữ liệu, hỏi ở năm ĐANG chạy — hai vế
        # cùng bật và cùng khớp, nên vẫn không dòng nào.
        assert rows_at("CURRENT_DATE") == []

        # Đối chứng 2: cho năm 2060 một dòng số dư ⇒ cổng #10 mở, và vì lượt
        # chuyển năm chưa dựng dòng cha cho khách này, khoản nợ 250.000 hiện ra
        # đúng như câu sẽ báo nếu không có cổng. Đây là thứ chứng minh cổng
        # đang làm việc chứ không phải câu luôn rỗng.
        session.add(
            OpeningBalance(
                fiscal_year_id=pending,
                ledger=0,
                branch_id=context.branch_id,
                account_id=context.accounts["111"],
                currency_code="VND",
                exchange_rate=Decimal(1),
                debit=Decimal(1),
                debit_fc=Decimal(1),
                detail_kind=0,
            )
        )
        session.flush()
        (uncovered,) = rows_at("DATE '2060-06-30'")
        assert Decimal(str(uncovered["difference"])) == Decimal(-250_000)

        session.execute(sa_delete(OpeningBalance).where(OpeningBalance.fiscal_year_id == pending))
        session.execute(sa_delete(GlPosting).where(GlPosting.voucher_id == voucher.id))
        account.detail_tracking = None
        session.flush()
        return None

    run(work)
