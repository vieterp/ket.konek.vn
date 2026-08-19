"""Nhập số dư ban đầu nhóm 0–4 trên PostgreSQL thật (slice 4C, bước 12).

Bốn nhóm bất biến, bám tiêu chí thành công phase-04:

* **Ghi đúng hình dạng**: dòng cha gộp theo (nhóm, đối tượng, TK, tiền tệ),
  hóa đơn treo dưới dòng cha (FR-OPB-003), hai sổ độc lập (FR-OPB-008).
* **Bàn giao 4B**: ghi số dư tự `mark_dirty` kỳ đầu năm — recalc thấy số mới
  mà KHÔNG cần đánh dấu tay (đảo của
  `test_first_period_opening_comes_from_opening_balances`).
* **Thay trọn theo nhóm**: nhập lại chỉ thay nhóm có mặt trong tệp.
* **Rào FR-OPB-011**: kỳ đầu năm khóa thì mọi đường ghi bị chặn.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from import_support import FakeProgress
from ket.kernel.attachments import storage
from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    ImportSourceNotValidatedError,
    OpeningPeriodLockedError,
)
from ket.kernel.jobs import queue
from ket.kernel.jobs.models import JobStatus
from ket.kernel.jobs.registry import JobContext
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.posting.balances import recalc
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances import import_job
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)
from ket.posting.opening_balances.report import OpeningImportReport
from ket.posting.opening_balances.template import (
    ACCOUNT_SHEET,
    PAYABLE_SHEET,
    RECEIVABLE_SHEET,
)
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_opening_balance_parsing import workbook_of

pytestmark = pytest.mark.db

ACTOR_ID = 1
VND = "VND"


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture
def catalog(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, str]:
    """Một khách hàng, một NCC, một nhân viên — mã duy nhất theo chi nhánh test."""
    suffix = context.branch_code
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        partners = MasterDataService(session, Partner)
        partners.create(code=f"KH-{suffix}", name="Khách test", extra={"is_customer": True})
        partners.create(code=f"NCC-{suffix}", name="NCC test", extra={"is_vendor": True})
        MasterDataService(session, Employee).create(code=f"NV-{suffix}", name="Nhân viên test")
    return {"customer": f"KH-{suffix}", "vendor": f"NCC-{suffix}", "employee": f"NV-{suffix}"}


def run_opening_import(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    storage_root: Path,
    source: BytesIO,
    *,
    ledger: int = Ledger.FINANCIAL,
    commit: bool = True,
) -> OpeningImportReport:
    """Một lượt kiểm/ghi trong transaction thật — qua đúng đường H85 của job."""
    stored = storage.store_stream(
        storage_root, dataset.schema_name, source, max_bytes=32 * 1024 * 1024
    )
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        job_context = JobContext(
            job_id=uuid4(),
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset.schema_name,
            branch_id=context.branch_id,
            requested_by=ACTOR_ID,
            storage_root=storage_root,
        )
        return import_job._run(
            job_context,
            fiscal_year_id=context.fiscal_year_id,
            ledger=ledger,
            content_hash=stored.content_hash,
            file_name="so-du.xlsx",
            commit=commit,
        )


def balanced_workbook(catalog: dict[str, str]) -> BytesIO:
    """111 nợ 1.000.000 ↔ 331 (NCC, 2 hóa đơn) có 1.000.000 — cân BR-OPB-01."""
    return workbook_of(
        {
            ACCOUNT_SHEET: [["111", None, None, None, None, 1_000_000, None]],
            PAYABLE_SHEET: [
                [
                    catalog["vendor"],
                    "331",
                    None,
                    None,
                    "HD-A",
                    "20/11/2025",
                    "20/01/2026",
                    None,
                    None,
                    None,
                    600_000,
                ],
                [
                    catalog["vendor"],
                    "331",
                    None,
                    None,
                    "HD-B",
                    "05/12/2025",
                    None,
                    None,
                    None,
                    None,
                    400_000,
                ],
            ],
        }
    )


def opening_rows(
    session: Session, context: PostingContext, *, ledger: int = Ledger.FINANCIAL
) -> list[OpeningBalance]:
    return list(
        session.execute(
            select(OpeningBalance)
            .where(OpeningBalance.fiscal_year_id == context.fiscal_year_id)
            .where(OpeningBalance.branch_id == context.branch_id)
            .where(OpeningBalance.ledger == ledger)
            .order_by(OpeningBalance.detail_kind, OpeningBalance.id)
        )
        .scalars()
        .all()
    )


def first_period_id(session: Session, context: PostingContext) -> int:
    return session.execute(
        select(AccountingPeriod.id)
        .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
        .order_by(AccountingPeriod.period_no)
        .limit(1)
    ).scalar_one()


def test_commit_writes_parents_invoices_marks_dirty_and_audits(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    report = run_opening_import(
        session_factory, dataset_alpha, context, tmp_path, balanced_workbook(catalog)
    )
    assert report.committed and report.is_valid
    assert report.rows_by_kind == {OpeningDetailKind.ACCOUNT: 1, OpeningDetailKind.PAYABLE: 1}
    assert report.invoice_rows == 2
    assert report.is_balanced

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        rows = opening_rows(session, context)
        assert len(rows) == 2
        payable = next(row for row in rows if row.detail_kind == OpeningDetailKind.PAYABLE)
        assert payable.partner_id is not None and payable.partner_kind == 1
        assert payable.credit == Decimal("1000000.00")
        invoices = list(
            session.execute(
                select(OpeningBalanceInvoice)
                .where(OpeningBalanceInvoice.opening_balance_id == payable.id)
                .order_by(OpeningBalanceInvoice.invoice_no)
            )
            .scalars()
            .all()
        )
        assert [invoice.amount for invoice in invoices] == [
            Decimal("600000.00"),
            Decimal("400000.00"),
        ]
        # BR-OPB-05 giữ nguyên ngày cũ; hạn thanh toán đi theo.
        assert invoices[0].invoice_date == date(2025, 11, 20)

        # Bàn giao 4B: dấu bẩn kỳ đầu năm phải có mà không ai đánh tay.
        mark = session.execute(
            select(BalanceRecalcQueue)
            .where(BalanceRecalcQueue.branch_id == context.branch_id)
            .where(BalanceRecalcQueue.ledger == Ledger.FINANCIAL)
        ).scalar_one()
        assert mark.from_period_id == first_period_id(session, context)

        audit = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "opening_balances")
                .where(AuditLog.action == AuditAction.IMPORTED.value)
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
        assert audit is not None
        assert audit.new_values is not None
        assert audit.new_values["content_hash"] == report.content_hash


def test_recalc_sees_imported_opening_without_manual_mark(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    """Đảo của test 4B `test_first_period_opening_comes_from_opening_balances`."""
    run_opening_import(
        session_factory, dataset_alpha, context, tmp_path, balanced_workbook(catalog)
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        marks = recalc.pending_marks(session, branch_id=context.branch_id)
        runs = recalc.build_runs(session, marks)
        for one_run in runs:
            for period in one_run.periods:
                recalc.recalc_period(
                    session, ledger=one_run.ledger, branch_id=context.branch_id, period=period
                )
        recalc.clear_marks(session, branch_id=context.branch_id, marks=marks)

        from ket.posting.balances.query_service import trial_balance

        result = trial_balance(
            session,
            ledger=Ledger.FINANCIAL,
            period_id=first_period_id(session, context),
            branch_id=context.branch_id,
        )
        assert not result.stale
        by_account = {row.account_code: row for row in result.rows}
        assert by_account["111"].opening_debit == Decimal("1000000.00")
        assert by_account["331"].opening_credit == Decimal("1000000.00")


def test_reimport_replaces_only_kinds_present_in_file(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    run_opening_import(
        session_factory, dataset_alpha, context, tmp_path, balanced_workbook(catalog)
    )
    second = workbook_of({ACCOUNT_SHEET: [["111", None, None, None, None, 2_000_000, None]]})
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, second)
    assert report.committed
    assert report.replaced_kinds == [OpeningDetailKind.ACCOUNT]
    # Tổng sau lượt ghi = 2.000.000 nợ mới + 1.000.000 có của nhóm KHÔNG bị thay.
    assert report.total_debit == Decimal("2000000.00")
    assert report.total_credit == Decimal("1000000.00")
    assert not report.is_balanced

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        rows = opening_rows(session, context)
        kinds = sorted(row.detail_kind for row in rows)
        assert kinds == [OpeningDetailKind.ACCOUNT, OpeningDetailKind.PAYABLE]
        account_row = next(row for row in rows if row.detail_kind == OpeningDetailKind.ACCOUNT)
        assert account_row.debit == Decimal("2000000.00")


def test_ledgers_hold_independent_openings(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,
        balanced_workbook(catalog),
        ledger=Ledger.FINANCIAL,
    )
    management = workbook_of({ACCOUNT_SHEET: [["111", None, None, None, None, 700_000, None]]})
    run_opening_import(
        session_factory,
        dataset_alpha,
        context,
        tmp_path,
        management,
        ledger=Ledger.MANAGEMENT,
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        financial = opening_rows(session, context, ledger=Ledger.FINANCIAL)
        managerial = opening_rows(session, context, ledger=Ledger.MANAGEMENT)
        assert len(financial) == 2
        assert len(managerial) == 1
        assert managerial[0].debit == Decimal("700000.00")


def test_dual_nature_partner_keeps_both_sides_on_one_parent(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    """Khách vừa còn nợ (2 hóa đơn) vừa ứng trước — 131 lưỡng tính (BR-OPB-03)."""
    source = workbook_of(
        {
            RECEIVABLE_SHEET: [
                [
                    catalog["customer"],
                    "131",
                    None,
                    None,
                    "HD-1",
                    "10/10/2025",
                    None,
                    None,
                    None,
                    800_000,
                    None,
                ],
                [
                    catalog["customer"],
                    "131",
                    None,
                    None,
                    "HD-2",
                    "12/10/2025",
                    None,
                    None,
                    None,
                    200_000,
                    None,
                ],
                [
                    catalog["customer"],
                    "131",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    300_000,
                ],
            ]
        }
    )
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    assert report.committed, [error.model_dump() for error in report.errors]
    assert report.rows_by_kind == {OpeningDetailKind.RECEIVABLE: 1}
    assert report.invoice_rows == 2

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        (row,) = opening_rows(session, context)
        assert row.debit == Decimal("1000000.00")
        assert row.credit == Decimal("300000.00")


def test_lookup_and_sheet_mismatch_errors(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    source = workbook_of(
        {
            # 131 theo dõi khách hàng → không được nhập ở sheet TK thường.
            ACCOUNT_SHEET: [
                ["131", None, None, None, None, 100, None],
                ["9999", None, None, None, None, 100, None],
                ["11", None, None, None, None, 100, None],
            ],
            RECEIVABLE_SHEET: [
                # Mã không tồn tại.
                ["KH-KHONG-CO", "131", None, None, "HD", "01/12/2025", None, None, None, 50, None],
                # Đối tác có thật nhưng là NCC, không phải khách hàng.
                [
                    catalog["vendor"],
                    "131",
                    None,
                    None,
                    "HD2",
                    "01/12/2025",
                    None,
                    None,
                    None,
                    50,
                    None,
                ],
            ],
        }
    )
    report = run_opening_import(
        session_factory, dataset_alpha, context, tmp_path, source, commit=True
    )
    codes = {error.code for error in report.errors}
    assert "opening.needs_partner_sheet" in codes
    assert "opening.account_unknown" in codes
    assert "opening.account_summary" in codes
    assert "opening.partner_unknown" in codes
    assert "opening.partner_not_customer" in codes
    assert not report.committed

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        assert opening_rows(session, context) == []


def test_duplicate_invoice_in_file_is_rejected(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    source = workbook_of(
        {
            PAYABLE_SHEET: [
                [
                    catalog["vendor"],
                    "331",
                    None,
                    None,
                    "HD-X",
                    "20/11/2025",
                    None,
                    None,
                    None,
                    None,
                    100,
                ],
                [
                    catalog["vendor"],
                    "331",
                    None,
                    None,
                    "HD-X",
                    "21/11/2025",
                    None,
                    None,
                    None,
                    None,
                    200,
                ],
            ]
        }
    )
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    assert {error.code for error in report.errors} == {"opening.duplicate_invoice"}
    assert not report.committed


def test_unbalanced_import_warns_but_still_commits(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: Path,
) -> None:
    """FR-OPB-006 nói *cảnh báo* khi lệch — chặn nằm ở cổng ghi sổ, không ở đây."""
    source = workbook_of({ACCOUNT_SHEET: [["111", None, None, None, None, 500_000, None]]})
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    assert report.committed
    assert any(warning.code == "opening.unbalanced" for warning in report.warnings)


def test_locked_first_period_blocks_import(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: Path,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        period_id = first_period_id(session, context)
        session.execute(
            update(AccountingPeriod)
            .where(AccountingPeriod.id == period_id)
            .values(locked_at=datetime.now(tz=UTC), locked_by=ACTOR_ID)
        )
    try:
        source = workbook_of({ACCOUNT_SHEET: [["111", None, None, None, None, 1, None]]})
        with pytest.raises(OpeningPeriodLockedError):
            run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    finally:
        with unit_of_work(session_factory, scope) as session:
            session.execute(
                update(AccountingPeriod)
                .where(AccountingPeriod.fiscal_year_id == context.fiscal_year_id)
                .values(locked_at=None, locked_by=None)
            )


def test_commit_requires_matching_validated_run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    catalog: dict[str, str],
    tmp_path: Path,
) -> None:
    """Chuỗi H78 thật: lượt ghi đọc tệp từ báo cáo lượt kiểm; (năm, sổ) lệch bị từ chối."""
    stored = storage.store_stream(
        tmp_path,
        dataset_alpha.schema_name,
        balanced_workbook(catalog),
        max_bytes=32 * 1024 * 1024,
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        validate_job = queue.enqueue(
            session,
            job_type=import_job.VALIDATE_OPENING_IMPORT,
            params={
                "content_hash": stored.content_hash,
                "file_name": "so-du.xlsx",
                "fiscal_year_id": context.fiscal_year_id,
                "ledger": int(Ledger.FINANCIAL),
            },
            requested_by=ACTOR_ID,
            branch_id=context.branch_id,
        )
        validate_id = validate_job.id

    # Chạy thân lượt kiểm rồi ghi kết quả vào dòng job — vai của worker.
    with unit_of_work(session_factory, scope) as session:
        job_context = JobContext(
            job_id=validate_id,
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=context.branch_id,
            requested_by=ACTOR_ID,
            storage_root=tmp_path,
        )
        result = import_job.run_validate(
            job_context,
            import_job.OpeningValidateParams(
                content_hash=stored.content_hash,
                file_name="so-du.xlsx",
                fiscal_year_id=context.fiscal_year_id,
                ledger=int(Ledger.FINANCIAL),
            ),
        )
        from ket.kernel.jobs.models import Job

        session.execute(
            update(Job)
            .where(Job.id == validate_id)
            .values(status=JobStatus.DONE.value, result=result)
        )

    with unit_of_work(session_factory, scope) as session:
        job_context = JobContext(
            job_id=uuid4(),
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=context.branch_id,
            requested_by=ACTOR_ID,
            storage_root=tmp_path,
        )
        with pytest.raises(ImportSourceNotValidatedError):
            import_job.run_commit(
                job_context,
                import_job.OpeningCommitParams(
                    validation_job_id=validate_id,
                    fiscal_year_id=context.fiscal_year_id,
                    ledger=int(Ledger.MANAGEMENT),
                ),
            )
        commit_result = import_job.run_commit(
            job_context,
            import_job.OpeningCommitParams(
                validation_job_id=validate_id,
                fiscal_year_id=context.fiscal_year_id,
                ledger=int(Ledger.FINANCIAL),
            ),
        )
        report = OpeningImportReport.model_validate(cast(dict[str, object], commit_result))
        assert report.committed


def test_aggregation_keeps_currency_and_rate_apart(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: Path,
) -> None:
    """Khóa gộp dòng cha phải mang cả tiền tệ lẫn tỷ giá (mutation M7/M8).

    Bỏ tiền tệ khỏi khóa là cộng 100 USD với 1.000 VND vào một dòng; bỏ tỷ giá
    là trộn hai lô ngoại tệ nhập hai thời điểm — cả hai đều "chạy êm" nếu không
    có một lượt nhập ngoại tệ thật đi qua đường tích hợp.
    """
    source = workbook_of(
        {
            ACCOUNT_SHEET: [
                ["112", "USD", 25_000, 100, None, 2_500_000, None],
                ["112", "USD", 26_000, 50, None, 1_300_000, None],
                # Cùng tỷ giá 1 với dòng VND bên dưới — cặp này là thứ bắt được
                # đột biến "khóa gộp bỏ tiền tệ" (M8): tỷ giá không phân biệt hộ.
                ["112", "USD", 1, 700, None, 700, None],
                ["112", None, None, None, None, 1_000, None],
            ]
        }
    )
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    assert report.committed, [error.model_dump() for error in report.errors]
    assert report.rows_by_kind == {OpeningDetailKind.ACCOUNT: 4}

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        rows = opening_rows(session, context)
        shape = sorted((row.currency_code, str(row.exchange_rate), str(row.debit)) for row in rows)
        assert shape == [
            ("USD", "1.000000", "700.00"),
            ("USD", "25000.000000", "2500000.00"),
            ("USD", "26000.000000", "1300000.00"),
            ("VND", "1.000000", "1000.00"),
        ]


def test_unknown_currency_is_rejected(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: Path,
) -> None:
    """Review 4C, M7: mã tiền không có trong danh mục không được ghi im lặng."""
    source = workbook_of({ACCOUNT_SHEET: [["111", "ZWL", 1_000, 5, None, 5_000, None]]})
    report = run_opening_import(session_factory, dataset_alpha, context, tmp_path, source)
    assert not report.committed
    assert any(error.code == "opening.currency_unknown" for error in report.errors)


def test_write_holds_period_share_lock_and_serialises_writers(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hai cơ chế RT-09 của đường ghi số dư phải CÓ MẶT, đo bằng hai kết nối thật.

    Review 4C, H3 (survivor 4A lặp lại): bỏ FOR SHARE hay bỏ advisory lock thì
    bộ test cũ vẫn xanh. Gate đặt TRƯỚC `mark_dirty` — sau đó thì FK
    `from_period_id` của hàng đợi giữ FOR KEY SHARE trên hàng kỳ và che mất
    FOR SHARE (cạm bẫy reviewer đã chỉ).

    Trong lúc một lượt ghi đang đứng giữa transaction:
    * lượt khóa sổ (`FOR UPDATE NOWAIT` trên hàng kỳ) phải bị chặn — chứng minh
      FOR SHARE đang được giữ;
    * một lượt ghi thứ hai (`pg_try_advisory_xact_lock` cùng tag) phải không
      lấy được khóa — chứng minh advisory lock đang được giữ.
    """
    import threading

    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import OperationalError

    from ket.posting.opening_balances import service as opening_service

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        period_id = first_period_id(session, context)

    in_write = threading.Event()
    release = threading.Event()
    real_mark_dirty = opening_service.mark_dirty

    def gated_mark_dirty(*args: object, **kwargs: object) -> None:
        in_write.set()
        assert release.wait(timeout=30), "test không nhả gate"
        real_mark_dirty(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(opening_service, "mark_dirty", gated_mark_dirty)

    outcome: dict[str, object] = {}

    def writer() -> None:
        try:
            outcome["report"] = run_opening_import(
                session_factory,
                dataset_alpha,
                context,
                tmp_path,
                workbook_of({ACCOUNT_SHEET: [["111", None, None, None, None, 42, None]]}),
            )
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert in_write.wait(timeout=30), f"lượt ghi không tới được gate: {outcome.get('error')}"

        tag = (
            "posting.opening_balances.write:"
            f"{dataset_alpha.schema_name}:{context.branch_id}:{context.fiscal_year_id}"
        )
        with unit_of_work(session_factory, scope) as session:
            got_lock = session.execute(
                sql_text("SELECT pg_try_advisory_xact_lock(hashtext(:tag)::bigint)"),
                {"tag": tag},
            ).scalar_one()
            assert got_lock is False, "advisory lock (năm, chi nhánh) không được giữ"

        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, scope) as session:
                session.execute(
                    sql_text(
                        "SELECT id FROM accounting_periods WHERE id = :period FOR UPDATE NOWAIT"
                    ),
                    {"period": period_id},
                )
    finally:
        release.set()
        thread.join(timeout=30)

    report = outcome.get("report")
    assert report is not None, outcome.get("error")
    assert isinstance(report, OpeningImportReport) and report.committed
