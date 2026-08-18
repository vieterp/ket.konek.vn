"""Hai loại job của nhập liệu Excel: kiểm và ghi (H78, bước 15).

Là job chứ không endpoint đồng bộ vì 10.000 dòng của FR-NFR-043 cần đúng ba thứ
hàng đợi đã có sẵn từ phase 2: thanh tiến độ, nút Hủy, và lease/reaper khi worker
chết giữa chừng. FR-NFR-044 ("giao diện không bị khóa") thì loại hẳn phương án
chạy trong tiến trình API.

**Báo cáo nằm ở `jobs.result`**, không ở một bảng riêng — đó là H78. `jobs` đã có
chủ sở hữu, phạm vi chi nhánh, quyền xem và một vòng đời; một bảng `import_batches`
song song sẽ phải phát minh lại cả bốn.

Hai loại chứ không một loại có cờ `commit`, vì quyền của chúng khác nhau: kiểm dữ
liệu là việc đọc, còn ghi thì tạo và sửa hàng nghìn bản ghi cùng lúc. Một loại
mang cả hai sẽ phải chọn một mức quyền, và mức nào cũng sai — lỏng thì ai cũng
ghi được, chặt thì không ai thử được tệp của mình trước khi nhờ người có quyền
bấm nút.

**Cả hai khai `direct_enqueue=False`**: mã quyền của một `JobType` là chuỗi tĩnh
nên nó không nói được "trên danh mục nào", và câu đó chỉ trả lời được ở
`routers/imports.py` — nơi có `slug`. Xem `JobType.direct_enqueue`.
"""

from __future__ import annotations

from typing import IO, Final
from uuid import UUID

from pydantic import BaseModel, Field

from ket.kernel.attachments import storage
from ket.kernel.errors import (
    DomainError,
    ImportFileMissingError,
    ImportSourceNotValidatedError,
)
from ket.kernel.excel.pipeline import run_import
from ket.kernel.excel.report import ImportMode, ImportReport, MissingReferenceMode
from ket.kernel.jobs.models import JobStatus, ResumeSemantics
from ket.kernel.jobs.queue import get_job
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.persistence.types import AuditValues
from ket.kernel.security.permissions import (
    MASTER_MODULE,
    Action,
    DocumentType,
    permission_code,
)
from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY

VALIDATE_CODE: Final[str] = "master.import.validate"
COMMIT_CODE: Final[str] = "master.import.commit"

IMPORT_DOCUMENT: Final[str] = "import"

IMPORT_VALIDATE: Final[str] = permission_code(MASTER_MODULE, IMPORT_DOCUMENT, Action.VIEW)
IMPORT_COMMIT: Final[str] = permission_code(MASTER_MODULE, IMPORT_DOCUMENT, Action.CREATE)
"""Hai mã quyền của **chức năng** nhập liệu, tách khỏi quyền trên từng danh mục.

Hai lớp, đúng khuôn `routers/jobs.py` đã dùng: mã ở đây trả lời "người này có
được dùng chức năng nhập liệu không", còn `spec.permission_code(...)` ở
`routers/imports.py` trả lời "trên danh mục nào". Chỉ có lớp thứ nhất thì ai
nhập được một danh mục sẽ nhập được mọi danh mục; chỉ có lớp thứ hai thì không
có cách nào tắt hẳn đường nhập liệu hàng loạt cho một vai trò."""

PERMISSION_REGISTRY.register(
    DocumentType(
        module=MASTER_MODULE,
        code=IMPORT_DOCUMENT,
        actions=frozenset({Action.VIEW, Action.CREATE}),
    )
)


class ImportValidateParams(BaseModel):
    """Tham số của lượt kiểm: tệp nào, danh mục nào, chế độ nào."""

    catalog: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    file_name: str = Field(min_length=1, max_length=255)
    mode: ImportMode = ImportMode.CREATE_ONLY
    """Mặc định `CREATE_ONLY` (H80) — chế độ ghi đè phải do người dùng chọn."""

    missing_reference: MissingReferenceMode = MissingReferenceMode.ERROR
    """Mã tra cứu không tìm thấy: báo lỗi, hay tự tạo (FR-NFR-062)."""

    allow_create_in: tuple[str, ...] = ()
    """Danh mục đích mà **endpoint đã kiểm quyền `create`** (H89).

    Thừa về mặt dữ liệu — thân job tự suy được danh sách danh mục *tự tạo được*
    từ registry — và đó chính là điểm: nó là bằng chứng rằng tầng HTTP đã hỏi
    lớp quyền per-danh-mục về **từng** danh mục đích. Thân job chỉ tạo trong danh
    sách này, nên không có đường nào tự tạo vào một danh mục mà người bấm nút
    không có quyền tạo.

    Cùng khuôn với `ImportCommitParams.catalog`: hai chỗ phải khớp, và chỗ duy
    nhất xếp hàng được job này là endpoint (`direct_enqueue=False`).
    """


class ImportCommitParams(BaseModel):
    """Tham số của lượt ghi: **trỏ vào một lượt kiểm đã thành công**.

    Không nhận lại `content_hash` từ client — nó được đọc ra từ báo cáo của lượt
    kiểm. Đó là toàn bộ giá trị của H78: nếu client khai lại hash thì "tệp đã
    kiểm chính là tệp sẽ ghi" quay về thành một lời hứa thay vì một tính chất.
    """

    validation_job_id: UUID

    allow_create_in: tuple[str, ...] = ()
    """Như ở `ImportValidateParams`: danh mục đích mà endpoint vừa kiểm quyền.

    Khai lại ở lượt **ghi** chứ không đọc từ báo cáo của lượt kiểm: quyền phải
    được kiểm ở đúng lúc bấm nút ghi, bởi đúng người bấm nút ấy. Lượt kiểm có
    thể do một người khác chạy từ hôm trước, và vai trò thì đổi được.
    """

    catalog: str = Field(min_length=1, max_length=100)
    """Danh mục mà **endpoint** đã kiểm quyền trên đó (H89).

    Thừa về mặt dữ liệu — báo cáo của lượt kiểm cũng mang tên danh mục — và đó
    chính là điểm: hai giá trị phải **khớp nhau**, và `run_commit` từ chối khi
    chúng lệch.

    Không có ràng buộc ấy thì hai chỗ nói về hai danh mục khác nhau mà không ai
    đối chiếu: endpoint `/master/warehouses/import/commit` đòi quyền tạo trên
    `warehouses`, còn thân job đọc `report.catalog` và ghi vào `payment_terms`.
    Hệ quả là leo quyền — chỉ cần quyền **xem** một danh mục (đủ để chạy lượt
    kiểm) cộng quyền tạo trên **một** danh mục bất kỳ là ghi được vào mọi danh
    mục còn lại.
    """


def _spec_of(slug: str) -> CatalogSpec:
    spec = CATALOG_REGISTRY.get(slug)
    if spec is None:
        raise ImportSourceNotValidatedError(
            "Danh mục không tồn tại trong bản cài này", catalog=slug
        )
    return spec


def _open_file(context: JobContext, content_hash: str) -> IO[bytes]:
    """Mở tệp đã lưu trong kho định địa chỉ theo nội dung, hoặc nói rõ nó không còn."""
    if context.storage_root is None:
        raise ImportFileMissingError("Bản cài chưa cấu hình thư mục tệp (KET_ATTACHMENTS_DIR)")
    try:
        return storage.open_blob(context.storage_root, context.dataset_schema, content_hash)
    except FileNotFoundError as error:
        raise ImportFileMissingError(
            "Tệp đã tải lên không còn trong kho — hãy tải lên lại",
            content_hash=content_hash,
        ) from error


def run_validate(context: JobContext, params: ImportValidateParams) -> JobResult:
    """Kiểm tệp, **không ghi gì** (FR-SYS-081)."""
    spec = _spec_of(params.catalog)
    with _open_file(context, params.content_hash) as source:
        report = run_import(
            context,
            spec=spec,
            source=source,
            file_name=params.file_name,
            content_hash=params.content_hash,
            mode=params.mode,
            commit=False,
            missing_reference=params.missing_reference,
            allow_create_in=frozenset(params.allow_create_in),
        )
    return _as_result(report)


def run_commit(context: JobContext, params: ImportCommitParams) -> JobResult:
    """Ghi tệp của một lượt kiểm đã thành công (H78, H85).

    Phép kiểm đầu tiên là **danh mục của lượt kiểm phải đúng danh mục mà endpoint
    đã xét quyền** (H89) — xem `ImportCommitParams.catalog`.
    """
    source_report = _validated_report(context, params.validation_job_id)
    if source_report.catalog != params.catalog:
        raise ImportSourceNotValidatedError(
            "Lượt kiểm dữ liệu thuộc một danh mục khác",
            job_id=str(params.validation_job_id),
            expected=params.catalog,
            found=source_report.catalog,
        )
    spec = _spec_of(source_report.catalog)
    with _open_file(context, source_report.content_hash) as source:
        report = run_import(
            context,
            spec=spec,
            source=source,
            file_name=source_report.file_name,
            content_hash=source_report.content_hash,
            mode=source_report.mode,
            commit=True,
            # Lấy lại từ **báo cáo của lượt kiểm**, không từ tham số của lượt
            # ghi: người dùng đã đọc con số "sẽ tạo thêm N" của đúng lượt kiểm
            # ấy, nên lượt ghi phải làm đúng thứ đã hứa. `_validated_report` đã
            # khẳng định báo cáo thuộc dataset này và đúng danh mục (H89).
            missing_reference=source_report.missing_reference,
            allow_create_in=frozenset(params.allow_create_in),
        )
    return _as_result(report)


def _validated_report(context: JobContext, validation_job_id: UUID) -> ImportReport:
    """Báo cáo của lượt kiểm được trỏ tới, sau khi khẳng định nó dùng được.

    Bốn điều kiện, và cả bốn đều là đường mà một lần gửi sai đi vào được: job
    phải tồn tại **trong dataset này** (`get_job` đã lọc theo phạm vi), phải là
    một lượt **kiểm**, phải đã **xong**, và báo cáo của nó phải **không còn dòng
    lỗi**. Thiếu điều kiện cuối thì "bắt buộc kiểm trước khi ghi" của FR-SYS-081
    tụt xuống thành "bắt buộc bấm nút kiểm trước khi ghi".
    """
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
    report = ImportReport.model_validate(job.result)
    if not report.is_valid:
        raise ImportSourceNotValidatedError(
            "Lượt kiểm dữ liệu còn dòng lỗi — sửa tệp rồi kiểm lại",
            job_id=str(validation_job_id),
            error_rows=report.error_rows,
        )
    return report


def _as_result(report: ImportReport) -> AuditValues:
    """Báo cáo → JSONB của `jobs.result`."""
    return dict(report.model_dump(mode="json"))


VALIDATE_IMPORT: Final[JobType[ImportValidateParams]] = JobType(
    code=VALIDATE_CODE,
    permission=IMPORT_VALIDATE,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=ImportValidateParams,
    handler=run_validate,
    direct_enqueue=False,
    description="Kiểm tra tệp Excel nhập liệu danh mục, không ghi dữ liệu",
)

COMMIT_IMPORT: Final[JobType[ImportCommitParams]] = JobType(
    code=COMMIT_CODE,
    permission=IMPORT_COMMIT,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=ImportCommitParams,
    handler=run_commit,
    direct_enqueue=False,
    description="Ghi dữ liệu từ tệp Excel đã kiểm vào danh mục",
)
"""`IDEMPOTENT_RESTART` chứ không `CHECKPOINTED`: cả lượt ghi nằm trong **một**
transaction, nên một lần chạy dở dang không để lại gì và chạy lại từ đầu là
đúng. Chạy lại một lượt đã xong cũng vô hại — `create_only` thì mọi dòng thành
lỗi trùng mã, `create_and_update` thì ghi lại đúng cùng giá trị (H85)."""

for _job_type in (VALIDATE_IMPORT, COMMIT_IMPORT):
    REGISTRY.register(_job_type)
