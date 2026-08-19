"""Hai job nhập số dư ban đầu: kiểm và ghi — cùng khuôn `excel/job.py` (H78/H85).

Là job vì cùng ba lý do của khung danh mục (tiến độ, nút hủy, lease khi worker
chết) cộng một lý do riêng: lượt ghi giữ `FOR SHARE` trên hàng kỳ (RT-09) và
advisory lock theo (năm, chi nhánh) — những khóa không được phép sống trong một
request HTTP treo theo mạng của client.

Hai loại chứ không một loại có cờ, đúng lý do phân quyền của khung danh mục:
kiểm là việc đọc (`posting.opening_balance.view`), ghi thay trọn số dư một năm
(`posting.opening_balance.create`).
"""

from __future__ import annotations

from typing import IO, Final
from uuid import UUID

from pydantic import BaseModel, Field

from ket.kernel.attachments import storage
from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    DomainError,
    ImportFileMissingError,
    ImportSourceNotValidatedError,
)
from ket.kernel.jobs.models import JobStatus, ResumeSemantics
from ket.kernel.jobs.queue import get_job
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.kernel.periods.models import FiscalYear
from ket.kernel.persistence.types import AuditValues
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.guards import guard_opening_writable
from ket.posting.opening_balances.parsing import parse_workbook
from ket.posting.opening_balances.report import OpeningImportReport
from ket.posting.opening_balances.service import validate_rows, write_staged

OPENING_PERMISSION_MODULE: Final[str] = "posting"
OPENING_PERMISSION_CODE: Final[str] = "opening_balance"

OPENING_VIEW: Final[str] = permission_code(
    OPENING_PERMISSION_MODULE, OPENING_PERMISSION_CODE, Action.VIEW
)
OPENING_WRITE: Final[str] = permission_code(
    OPENING_PERMISSION_MODULE, OPENING_PERMISSION_CODE, Action.CREATE
)

VALIDATE_CODE: Final[str] = "posting.opening_balances.import.validate"
COMMIT_CODE: Final[str] = "posting.opening_balances.import.commit"


class OpeningValidateParams(BaseModel):
    """Tham số lượt kiểm: tệp nào, đổ vào (năm, sổ) nào."""

    content_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    file_name: str = Field(min_length=1, max_length=255)
    fiscal_year_id: int = Field(ge=1)
    ledger: int = Field(default=Ledger.FINANCIAL, ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)


class OpeningCommitParams(BaseModel):
    """Lượt ghi trỏ vào một lượt kiểm đã đạt — không nhận lại tệp từ client (H78).

    `fiscal_year_id`/`ledger` khai lại và phải **khớp báo cáo của lượt kiểm**
    (khuôn H89): endpoint xét quyền tại lúc bấm ghi, và hai giá trị lệch nghĩa
    là người bấm đang ghi vào một (năm, sổ) khác thứ họ vừa được duyệt.
    """

    validation_job_id: UUID
    fiscal_year_id: int = Field(ge=1)
    ledger: int = Field(default=Ledger.FINANCIAL, ge=Ledger.FINANCIAL, le=Ledger.MANAGEMENT)


def _fiscal_year_of(context: JobContext, fiscal_year_id: int) -> FiscalYear:
    fiscal_year = context.session.get(FiscalYear, fiscal_year_id)
    if fiscal_year is None:
        raise DomainError("Năm tài chính không tồn tại", fiscal_year_id=fiscal_year_id)
    return fiscal_year


def _open_file(context: JobContext, content_hash: str) -> IO[bytes]:
    """Cùng hợp đồng với `excel/job._open_file` — tệp nằm trong kho theo nội dung."""
    if context.storage_root is None:
        raise ImportFileMissingError("Bản cài chưa cấu hình thư mục tệp (KET_ATTACHMENTS_DIR)")
    try:
        return storage.open_blob(context.storage_root, context.dataset_schema, content_hash)
    except FileNotFoundError as error:
        raise ImportFileMissingError(
            "Tệp đã tải lên không còn trong kho — hãy tải lên lại",
            content_hash=content_hash,
        ) from error


def _run(
    context: JobContext,
    *,
    fiscal_year_id: int,
    ledger: int,
    content_hash: str,
    file_name: str,
    commit: bool,
) -> OpeningImportReport:
    """Một lượt trọn: đọc → kiểm → (ghi). `commit` là khác biệt duy nhất (H85)."""
    if context.branch_id is None:
        raise DomainError("Lượt nhập số dư phải được xếp hàng từ một chi nhánh đang thao tác")
    session = context.session
    fiscal_year = _fiscal_year_of(context, fiscal_year_id)
    # Lượt kiểm báo sớm kỳ đã khóa (không giữ khóa); lượt ghi sẽ giữ `FOR SHARE`
    # trong `write_staged` — kiểm ở đây để người dùng biết trước khi sửa tệp.
    guard_opening_writable(session, fiscal_year, for_share=False)

    scale = value_of(session, key=MONEY_SCALE_KEY, user_id=context.requested_by)
    if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
        raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")

    report = OpeningImportReport(
        fiscal_year_id=fiscal_year.id,
        ledger=ledger,
        file_name=file_name,
        content_hash=content_hash,
    )
    with _open_file(context, content_hash) as source:
        rows, failed = parse_workbook(
            source,
            base_currency=fiscal_year.base_currency,
            money_scale=scale,
            fiscal_year_start=fiscal_year.start_date,
            report=report,
        )
    context.progress.report(40, f"Đã đọc {report.total_rows} dòng")
    if not rows:
        raise DomainError("Tệp không có dòng dữ liệu nào — điền ít nhất một sheet số dư")

    staged = validate_rows(
        session,
        fiscal_year=fiscal_year,
        ledger=ledger,
        branch_id=context.branch_id,
        rows=rows,
        failed=failed,
        report=report,
    )
    context.progress.report(80, f"Đã kiểm {report.total_rows} dòng")

    if commit and report.is_valid:
        write_staged(
            session,
            staged=staged,
            fiscal_year=fiscal_year,
            ledger=ledger,
            branch_id=context.branch_id,
            dataset_schema=context.dataset_schema,
            job_id=context.job_id,
            file_name=file_name,
            content_hash=content_hash,
        )
        report.committed = True
    context.progress.report(100, _summary(report))
    return report


def _summary(report: OpeningImportReport) -> str:
    if not report.is_valid:
        return f"{report.error_rows}/{report.total_rows} dòng có lỗi — chưa ghi gì"
    balance_note = "" if report.is_balanced else " (chưa cân Nợ/Có)"
    if not report.committed:
        total = sum(report.rows_by_kind.values())
        return f"Hợp lệ: sẽ ghi {total} dòng số dư{balance_note}"
    return f"Đã ghi {sum(report.rows_by_kind.values())} dòng số dư{balance_note}"


def run_validate(context: JobContext, params: OpeningValidateParams) -> JobResult:
    """Kiểm tệp số dư, không ghi gì (FR-SYS-081 áp cho số dư ban đầu)."""
    report = _run(
        context,
        fiscal_year_id=params.fiscal_year_id,
        ledger=params.ledger,
        content_hash=params.content_hash,
        file_name=params.file_name,
        commit=False,
    )
    return _as_result(report)


def run_commit(context: JobContext, params: OpeningCommitParams) -> JobResult:
    """Ghi tệp của một lượt kiểm đã đạt — kiểm lại toàn bộ trước khi ghi (H85)."""
    source_report = _validated_report(context, params.validation_job_id)
    if (
        source_report.fiscal_year_id != params.fiscal_year_id
        or source_report.ledger != params.ledger
    ):
        raise ImportSourceNotValidatedError(
            "Lượt kiểm thuộc một (năm tài chính, sổ) khác",
            job_id=str(params.validation_job_id),
        )
    report = _run(
        context,
        fiscal_year_id=params.fiscal_year_id,
        ledger=params.ledger,
        content_hash=source_report.content_hash,
        file_name=source_report.file_name,
        commit=True,
    )
    return _as_result(report)


def _validated_report(context: JobContext, validation_job_id: UUID) -> OpeningImportReport:
    """Cùng bốn điều kiện với `excel/job._validated_report`."""
    try:
        job = get_job(context.session, validation_job_id)
    except DomainError as error:
        raise ImportSourceNotValidatedError(
            "Không tìm thấy lượt kiểm dữ liệu tương ứng",
            job_id=str(validation_job_id),
        ) from error
    if job.type != VALIDATE_CODE or job.status != JobStatus.DONE.value or job.result is None:
        raise ImportSourceNotValidatedError(
            "Lượt kiểm dữ liệu chưa hoàn tất — hãy kiểm lại trước khi nhập khẩu",
            job_id=str(validation_job_id),
        )
    report = OpeningImportReport.model_validate(job.result)
    if not report.is_valid:
        raise ImportSourceNotValidatedError(
            "Lượt kiểm dữ liệu còn dòng lỗi — sửa tệp rồi kiểm lại",
            job_id=str(validation_job_id),
            error_rows=report.error_rows,
        )
    return report


def _as_result(report: OpeningImportReport) -> AuditValues:
    return dict(report.model_dump(mode="json"))


VALIDATE_OPENING_IMPORT: Final[JobType[OpeningValidateParams]] = JobType(
    code=VALIDATE_CODE,
    permission=OPENING_VIEW,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=OpeningValidateParams,
    handler=run_validate,
    direct_enqueue=False,
    description="Kiểm tra tệp Excel số dư ban đầu, không ghi dữ liệu",
)

COMMIT_OPENING_IMPORT: Final[JobType[OpeningCommitParams]] = JobType(
    code=COMMIT_CODE,
    permission=OPENING_WRITE,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=OpeningCommitParams,
    handler=run_commit,
    direct_enqueue=False,
    description="Ghi số dư ban đầu từ tệp Excel đã kiểm",
)
"""`IDEMPOTENT_RESTART`: lượt ghi thay trọn các nhóm có mặt trong tệp trong một
transaction — chạy lại từ đầu cho đúng cùng kết quả."""

for _job_type in (VALIDATE_OPENING_IMPORT, COMMIT_OPENING_IMPORT):
    REGISTRY.register(_job_type)
