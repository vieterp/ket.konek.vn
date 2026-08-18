"""Nhập liệu danh mục từ Excel — ba endpoint cho mỗi danh mục, sinh từ registry.

| Đường dẫn | Việc |
| --- | --- |
| `GET  /api/v1/master/{slug}/template` | Tải tệp mẫu (FR-SYS-080) |
| `POST /api/v1/master/{slug}/import/validate` | Tải tệp lên + xếp hàng lượt kiểm (FR-SYS-081) |
| `POST /api/v1/master/{slug}/import/commit` | Xếp hàng lượt ghi từ một lượt kiểm đã đạt |

Sinh cho **từng** danh mục thay vì một bộ route có `{slug}` là tham số, cùng lý
do H47 đã ghi cho `routers/master_data.py`: mã quyền và nhãn tiếng Việt của mỗi
danh mục là tĩnh, nên chúng vào thẳng được đặc tả OpenAPI — client thấy "Vật tư
hàng hóa — tải tệp mẫu" chứ không thấy một endpoint chung phải tự đoán.

**Hai lớp quyền**, đúng khuôn `routers/jobs.py`: `master.import.*` là quyền dùng
chức năng nhập liệu, `master.{slug}.*` là quyền trên chính danh mục đó. Cả hai
đều kiểm ở đây, và hai loại job khai `direct_enqueue=False` nên không có đường
vòng qua endpoint hàng đợi chung.

Tệp ghi xuống kho **trước** khi xếp hàng, và ghi bằng chính
`attachments/storage.py`: nó đã định địa chỉ theo nội dung, đã tách theo schema
dataset, đã băm trong lúc ghi và đã chặn trần trong lúc ghi. Viết một kho thứ
hai cho tệp nhập liệu là chép lại bốn tính chất ấy để rồi thiếu một cái.
"""

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.routers.imports_schemas import (
    ImportCommitRequest,
    ImportCommitResponse,
    ImportValidateResponse,
)
from ket.kernel.attachments import storage
from ket.kernel.errors import AttachmentStorageNotConfiguredError
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.job import (
    COMMIT_IMPORT,
    IMPORT_COMMIT,
    IMPORT_VALIDATE,
    VALIDATE_IMPORT,
)
from ket.kernel.excel.report import ImportMode
from ket.kernel.excel.template import build_template, template_file_name
from ket.kernel.jobs import queue
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action
from ket.settings import Settings

router = APIRouter(prefix="/api/v1/master", tags=["master-data-import"])

XLSX_MEDIA_TYPE: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

IMPORT_MAX_BYTES: Final[int] = 32 * 1024 * 1024
"""Trần dung lượng một tệp nhập liệu.

Rộng hơn trần đính kèm mặc định vì một tệp 10.000 dòng × 15 cột có thật, nhưng
vẫn là một trần: `reader.MAX_ROWS` chặn số dòng *sau* khi đã đọc, còn đây chặn
byte *trong lúc* ghi — thứ duy nhất bảo vệ được đĩa máy chủ khỏi một lượt tải
lên nói dối `Content-Length`."""


def _storage_root(settings: Settings) -> Path:
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp (KET_ATTACHMENTS_DIR)"
        )
    return settings.attachments_dir


def _mount(spec: CatalogSpec) -> None:
    """Gắn ba endpoint nhập liệu của một danh mục."""
    descriptor = template_for(spec)

    @router.get(
        f"/{spec.slug}/template",
        summary=f"{spec.title} — tải tệp mẫu nhập liệu",
        response_class=StreamingResponse,
        responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Tệp .xlsx"}},
    )
    def download_template(
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.VIEW)))
        ],
    ) -> Response:
        """Tệp mẫu của danh mục này: sheet Hướng dẫn + sheet dữ liệu (FR-SYS-080).

        Quyền `view` chứ không `create`: tệp mẫu **không** chứa dữ liệu, nó chỉ
        mô tả hình dạng. Đòi quyền tạo ở đây sẽ chặn đúng người hay chuẩn bị tệp
        rồi chuyển cho kế toán trưởng bấm nút.

        Dựng lại mỗi lần gọi thay vì cache: nó là vài chục kilobyte sinh từ một
        descriptor nằm sẵn trong bộ nhớ, và một bản cache là một chỗ để tệp mẫu
        cũ sống sót qua lần thêm cột ở phase 5.
        """
        content = build_template(spec, descriptor)
        return Response(
            content=content,
            media_type=XLSX_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{template_file_name(spec)}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(
        f"/{spec.slug}/import/validate",
        response_model=ImportValidateResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary=f"{spec.title} — kiểm tra tệp nhập liệu",
    )
    def validate_import(
        authorized: Annotated[AuthorizedRequest, Depends(require_permission(IMPORT_VALIDATE))],
        factory: SessionFactory,
        settings: AppSettings,
        file: Annotated[UploadFile, File()],
        mode: Annotated[ImportMode, Form()] = ImportMode.CREATE_ONLY,
    ) -> ImportValidateResponse:
        """Tải tệp lên và xếp hàng lượt kiểm — **không ghi gì** (FR-SYS-081).

        `202`: yêu cầu đã ghi nhận, kết quả chưa có. Báo cáo lỗi theo dòng nằm ở
        `jobs.result` khi job xong.

        Chế độ mặc định là `create_only` (H80). Client phải gửi tường minh
        `mode=create_and_update` để bật đường ghi đè, và màn hình chỉ nên gửi nó
        sau khi người dùng đọc con số "sẽ cập nhật N dòng" của một lượt kiểm
        trước đó.
        """
        authorized.access.require(spec.permission_code(Action.VIEW))
        stored = storage.store_stream(
            _storage_root(settings),
            authorized.scope.dataset_schema,
            file.file,
            max_bytes=IMPORT_MAX_BYTES,
        )
        file_name = (file.filename or "nhap-lieu.xlsx")[:255]
        with unit_of_work(factory, authorized.scope) as session:
            job = queue.enqueue(
                session,
                job_type=VALIDATE_IMPORT,
                params={
                    "catalog": spec.slug,
                    "content_hash": stored.content_hash,
                    "file_name": file_name,
                    "mode": mode.value,
                },
                requested_by=authorized.scope.user_id,
                branch_id=authorized.scope.acting_branch_id,
            )
            return ImportValidateResponse(
                job_id=job.id,
                catalog=spec.slug,
                file_name=file_name,
                content_hash=stored.content_hash,
                mode=mode,
            )

    @router.post(
        f"/{spec.slug}/import/commit",
        response_model=ImportCommitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary=f"{spec.title} — ghi dữ liệu đã kiểm",
    )
    def commit_import(
        payload: ImportCommitRequest,
        authorized: Annotated[AuthorizedRequest, Depends(require_permission(IMPORT_COMMIT))],
        factory: SessionFactory,
    ) -> ImportCommitResponse:
        """Xếp hàng lượt ghi cho một lượt kiểm đã đạt (H78).

        Quyền `create` của chính danh mục, không phải `edit`: một lượt nhập tạo
        ra hàng nghìn bản ghi mới, và người được sửa tên một mã hàng không đương
        nhiên được tạo thêm mười nghìn mã hàng.

        Thân job kiểm lại rằng lượt kiểm được trỏ tới **thuộc dataset này, đúng
        loại, đã xong và không còn dòng lỗi** — bốn điều kiện ấy nằm ở
        `excel/job.py` chứ không ở đây, vì chúng đọc `jobs.result` và đó là việc
        của thân job, không của tầng HTTP.
        """
        authorized.access.require(spec.permission_code(Action.CREATE))
        with unit_of_work(factory, authorized.scope) as session:
            job = queue.enqueue(
                session,
                job_type=COMMIT_IMPORT,
                params={
                    "validation_job_id": str(payload.validation_job_id),
                    # Danh mục mà lớp quyền ngay trên vừa xét (H89). Thân job từ
                    # chối nếu lượt kiểm được trỏ tới thuộc danh mục khác.
                    "catalog": spec.slug,
                },
                requested_by=authorized.scope.user_id,
                branch_id=authorized.scope.acting_branch_id,
            )
            return ImportCommitResponse(job_id=job.id, catalog=spec.slug)


for _spec in CATALOG_REGISTRY.specs():
    _mount(_spec)
