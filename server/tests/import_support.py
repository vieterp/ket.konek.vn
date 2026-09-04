"""Khung chạy một lượt nhập liệu trên PostgreSQL thật, dùng chung giữa hai tệp test.

Tách ra khi lát 3C-2 thêm tệp test thứ hai chạy `run_import` (`test_export.py`
đo vòng **xuất → nhập lại**, FR-NFR-061). Cùng lý do `catalog_api_support.py`
được tách ở lát 3B-2: bản sao thứ hai của một khung dựng bối cảnh là chỗ hai bản
lệch nhau, và cả hai tệp vẫn xanh cho tới lúc một trong hai đo sai điều nó tưởng
đang đo — ở đây là `JobContext` thiếu một trường mà thân job vừa bắt đầu đọc.

Chỉ có phần **dựng bối cảnh**. Mọi khẳng định nằm trong từng tệp test.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import IO
from uuid import UUID

from openpyxl import Workbook
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.pipeline import run_import
from ket.kernel.excel.report import ImportMode, ImportReport, MissingReferenceMode
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.jobs.registry import JobContext
from ket.kernel.master_data.registry import REGISTRY, CatalogSpec
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.types import AuditValues
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work


@dataclass
class FakeProgress:
    """Bản giả của `JobProgress` — ghi lại thay vì chạm DB.

    Đủ cho lát này: thân job chỉ **báo** tiến độ, nó không đọc lại gì. Bản thật
    dùng một connection riêng và đã có test của nó ở `test_worker_runner.py`.
    """

    reports: list[tuple[int, str | None]]
    cancelled: bool = False

    def report(self, percent: int, message: str | None = None) -> None:
        self.reports.append((percent, message))

    def cancel_requested(self) -> bool:
        return self.cancelled

    def checkpoint(self, state: AuditValues) -> None:  # pragma: no cover — job này không dùng
        raise AssertionError("nhập liệu không khai `checkpointed`")


def spec_of(slug: str) -> CatalogSpec:
    spec = REGISTRY.get(slug)
    assert spec is not None, f"test viết cho danh mục {slug} nhưng nó không còn đăng ký"
    return spec


MINIMUM_VALID_CELLS: dict[str, dict[str, object]] = {
    # Danh mục có ràng buộc mà một dòng "chỉ mã và tên" không thỏa. Khai ở đây
    # thay vì nới lỏng phép khẳng định: nếu phase 5 thêm
    # một danh mục có `CHECK` riêng thì test này đỏ, và người thêm phải nói ra
    # đâu là dòng hợp lệ tối thiểu của nó — đúng thứ người viết tệp mẫu cần biết.
    "partners": {"is_customer": "x"},  # CHECK (is_group OR is_customer OR is_vendor)
    "payment_terms": {"due_days": "0"},  # CHECK (due_days >= 0), NOT NULL
    "items": {"nature": "service"},  # dịch vụ: không cần đơn vị chính, không cần kho
    # CHECK (is_group OR direction IS NOT NULL) — cùng hình dạng `nature` của vật
    # tư: cột để trống hợp lệ với nút nhóm, bắt buộc với bảng giá thật.
    "price_lists": {"direction": "1"},
    # Tham chiếu nhập theo MÃ của danh mục đích (`bank_code`, không `bank_id`).
    "company_bank_accounts": {"bank_code": "NH_MIN_IMPORT"},  # ngân hàng bắt buộc (6A)
}

MINIMUM_VALID_TARGETS: dict[str, tuple[tuple[str, str], ...]] = {
    # Danh mục mà dòng tối thiểu TRỎ SANG danh mục khác qua cột bắt buộc —
    # `import_source` gieo sẵn bản ghi đích (idempotent) trước khi chạy, vì
    # lượt nhập mặc định chạy `MissingReferenceMode.ERROR`. Tài khoản ngân
    # hàng doanh nghiệp (6A) là danh mục đầu tiên có tham chiếu bắt buộc.
    "company_bank_accounts": (("banks", "NH_MIN_IMPORT"),),
}


def minimal_row(spec: CatalogSpec, code: str, name: str) -> list[object]:
    """Dòng hợp lệ **tối thiểu** của một danh mục bất kỳ.

    Mã và tên cho mọi danh mục, cộng phần khai thêm ở `MINIMUM_VALID_CELLS` cho
    danh mục nào có ràng buộc liên-trường. Mọi ô còn lại để trống — đó là điều
    kiện để câu `INSERT` vẫn phải lập kế hoạch với **đầy đủ** cột, tức là vẫn
    bắt được lỗi ép kiểu dù giá trị rỗng.
    """
    descriptor = template_for(spec)
    extra = MINIMUM_VALID_CELLS.get(spec.slug, {})
    values: list[object] = [None] * len(descriptor.columns)
    for index, column in enumerate(descriptor.columns):
        if column.field == "code":
            values[index] = code
        elif column.field == "name":
            values[index] = name
        elif column.field in extra:
            values[index] = extra[column.field]
    return values


def workbook_bytes(slug: str, rows: list[list[object]]) -> BytesIO:
    """Tệp đúng cấu trúc mẫu, kèm các dòng đã cho."""
    descriptor = template_for(spec_of(slug))
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = descriptor.sheet_name
    sheet.append(list(descriptor.headers))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


LAST_JOB_ID: list[UUID] = []
"""`job_id` của lượt nhập gần nhất do `import_source` chạy.

Tồn tại cho đúng một loại khẳng định: test nào cần soi `audit_log` phải lọc theo
**chính lượt nhập của nó**. `audit_log` là bảng dùng chung cả phiên, và
`_record_import` ghi một dòng `IMPORTED` mang `entity_type` của danh mục mỗi lần
**bất kỳ** test nào nhập danh mục ấy — nên "dòng mới nhất" là một khẳng định về
thứ tự thu thập tệp test, không phải về mã (R3-H4).
"""


def import_source(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    scope: RequestScope,
    storage_root: Path,
    slug: str,
    source: IO[bytes],
    *,
    mode: ImportMode = ImportMode.CREATE_ONLY,
    commit: bool = False,
    branch_id: int | None = None,
    missing_reference: MissingReferenceMode = MissingReferenceMode.ERROR,
    allow_create_in: frozenset[str] = frozenset(),
    progress: FakeProgress | None = None,
) -> ImportReport:
    """Chạy một lượt nhập trong một transaction thật, trả về báo cáo.

    Mỗi lượt có `job_id` riêng và một dòng `jobs` thật: bảng đệm có khóa ngoại
    tới `jobs`, nên một id bịa ra sẽ đổ ở ràng buộc chứ không ở luật đang đo.
    """
    with unit_of_work(session_factory, scope) as session:
        for target_slug, target_code in MINIMUM_VALID_TARGETS.get(slug, ()):
            target_spec = spec_of(target_slug)
            service = MasterDataService(session, target_spec.model)
            try:
                service.resolve_by_code(target_code)
            except MasterDataNotFoundError:
                service.create(code=target_code, name=f"Bản ghi đích {target_code}")

    with unit_of_work(session_factory, scope) as session:
        job = queue.enqueue(session, job_type=SLOW_TASK, params={}, requested_by=1)
        job_id = job.id
    LAST_JOB_ID.append(job_id)
    with unit_of_work(session_factory, scope) as session:
        context = JobContext(
            job_id=job_id,
            session=session,
            progress=progress if progress is not None else FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset.schema_name,
            branch_id=branch_id,
            requested_by=1,
            storage_root=storage_root,
        )
        return run_import(
            context,
            spec=spec_of(slug),
            source=source,
            file_name="nhap-lieu.xlsx",
            content_hash="0" * 64,
            mode=mode,
            commit=commit,
            missing_reference=missing_reference,
            allow_create_in=allow_create_in,
        )
