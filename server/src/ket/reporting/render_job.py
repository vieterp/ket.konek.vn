"""Job nền `reporting.report.render` — đường chạy của báo cáo lớn (bước 19).

Vì sao là job: spike S2 (ADR-009) đo PDF ~3.000 dòng đã chạm mốc 10 giây của
FR-NFR-041, còn Sổ Cái cả năm là hàng phút — FR-NFR-042/044 cấm giữ một request
HTTP lâu như thế, và render đồng bộ còn giữ transaction + RAM suốt lượt chạy
(M2 review 5C). Endpoint `POST /reports/{code}/render` tự ước lượng số dòng và
xếp job này khi vượt ngưỡng (`report.pdf_job_threshold_rows` /
`report.xlsx_job_threshold_rows`); client theo dõi qua `/api/v1/jobs/{id}` rồi
tải tệp ở `GET /reports/render-jobs/{id}/file`.

Tệp kết quả ghi vào **kho blob content-addressed** (cùng kho với tệp đính kèm,
`kernel.attachments.storage`) chứ không vào `jobs.result` — JSONB không phải
chỗ cho một PDF trăm trang. `jobs.result` chỉ mang *tham chiếu*: hash, tên
tệp, kiểu nội dung. Render là phép ĐỌC nên `idempotent-restart` đúng theo
nghĩa đen: chạy lại từ đầu chỉ tốn thời gian, không nhân đôi trạng thái nào;
blob trùng nội dung tự khử theo hash.

`direct_enqueue=False`: không phải vì thiếu quyền tĩnh (`reporting.report.
export` trả lời trọn câu hỏi ai được kết xuất) mà vì tham số chỉ kiểm được
**đủ** khi có session — mã báo cáo, bộ tham số theo `param_set.spec`, và ước
lượng ngưỡng đều cần metadata trong DB. Đường vào duy nhất là endpoint render,
nơi mọi thứ đó đã kiểm xong trước khi job chạm hàng đợi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import Final, Literal

from pydantic import BaseModel, Field, JsonValue

from ket.kernel.attachments.storage import store_stream
from ket.kernel.errors import AttachmentStorageNotConfiguredError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import (
    REGISTRY,
    JobBranchScope,
    JobCancelled,
    JobContext,
    JobResult,
    JobType,
)
from ket.reporting.engine import REPORT_EXPORT
from ket.reporting.engine.engine import estimate_report_rows, render_report
from ket.reporting.rendering.options_factory import build_render_options

RENDER_JOB_CODE: Final[str] = "reporting.report.render"

RENDER_MAX_BYTES: Final[int] = 512 * 1024 * 1024
"""Trần kích thước tệp kết quả — hàng rào chống đầy đĩa, không phải giới hạn
nghiệp vụ. Spike S2: PDF 50.000 dòng ~ chục MB; 512 MiB là mức mà một dataset
sai (tích Descartes do thiếu điều kiện JOIN) mới chạm tới."""

CANCEL_CHECK_EVERY_ROWS: Final[int] = 1000
"""Nhịp kiểm cờ hủy + báo tiến độ = một lô cursor (`FETCH_BATCH_SIZE`).

Render không có trạng thái dở dang cần bảo vệ, nhưng kiểm mỗi dòng là một
round-trip DB mỗi dòng (cờ hủy đọc từ DB) — nhịp theo lô giữ lời hứa "Hủy có
hiệu lực trong ~1 giây" mà không biến worker thành máy phát truy vấn."""


class ReportRenderJobParams(BaseModel):
    """Tham số đã qua kiểm của endpoint render — xem docstring module."""

    code: str = Field(min_length=1, max_length=50)
    format: Literal["pdf", "xlsx"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


def _run_render(context: JobContext, params: ReportRenderJobParams) -> JobResult:
    if context.storage_root is None:
        raise AttachmentStorageNotConfiguredError(
            "Báo cáo chạy nền cần thư mục kho tệp — đặt KET_ATTACHMENTS_DIR cho worker"
        )
    session = context.session
    total = estimate_report_rows(session, code=params.code, raw_params=params.params)
    context.progress.report(1, f"Chuẩn bị kết xuất {total:,} dòng".replace(",", "."))

    def row_hook(index: int) -> None:
        if index % CANCEL_CHECK_EVERY_ROWS != 0:
            return
        if context.progress.cancel_requested():
            raise JobCancelled
        # Dành 5% đầu cho ước lượng và 5% cuối cho pha giấy + ghi tệp: thanh
        # tiến độ không được đứng ở 100% trong lúc WeasyPrint còn dàn trang.
        percent = 5 + (85 * index) // total if total else 50
        context.progress.report(min(percent, 90), f"Đã đọc {index:,} dòng".replace(",", "."))

    options = build_render_options(
        session,
        storage_root=context.storage_root,
        dataset_schema=context.dataset_schema,
        user_id=context.requested_by,
    )
    rendered = render_report(
        session,
        code=params.code,
        output_format=params.format,
        raw_params=params.params,
        # Giờ ĐỊA PHƯƠNG của máy chủ — cùng quyết định với endpoint render
        # đồng bộ: bản in tại Việt Nam ghi ngày Việt Nam.
        today=datetime.now(UTC).astimezone().date(),
        options=options,
        row_hook=row_hook,
    )
    context.progress.report(95, "Đang ghi tệp kết quả")
    stored = store_stream(
        context.storage_root,
        context.dataset_schema,
        BytesIO(rendered.content),
        max_bytes=RENDER_MAX_BYTES,
    )
    return {
        "content_hash": stored.content_hash,
        "file_name": rendered.filename,
        "media_type": rendered.media_type,
        "byte_size": stored.byte_size,
        "row_count": total,
    }


RENDER_JOB: Final[JobType[ReportRenderJobParams]] = JobType(
    code=RENDER_JOB_CODE,
    permission=REPORT_EXPORT,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=ReportRenderJobParams,
    handler=_run_render,
    # Job ĐỌC xuyên chi nhánh (C1 review 5E): thân job phải chạy dưới toàn bộ
    # chi nhánh hiện hành của người yêu cầu — đúng phạm vi mà lượt render đồng
    # bộ cùng người cùng tham số chạy dưới — chứ không phải một chi nhánh đang
    # thao tác ghi trên dòng job. Xem `worker.runner.resolve_body_scope`.
    branch_scope=JobBranchScope.REQUESTER_BRANCHES,
    description="Kết xuất báo cáo lớn (PDF/XLSX) trong nền",
    direct_enqueue=False,
)

REGISTRY.register(RENDER_JOB)
