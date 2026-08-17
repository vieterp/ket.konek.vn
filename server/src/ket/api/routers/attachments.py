"""Tệp đính kèm qua HTTP (`/api/v1/attachments`) — FR-NFR-053.

Bốn việc: đính một tệp vào bản ghi, xem bản ghi đang có tệp gì, tải một tệp về,
gỡ một tệp ra. Nội dung tệp nằm trên đĩa (`kernel/attachments/storage.py`), bảng
chỉ giữ metadata.

**Đường tải về luôn là `Content-Disposition: attachment` kèm `nosniff`**, kể cả
với PDF hay ảnh. Người dùng tải lên được tệp bất kỳ — trong đó có HTML — và chế
độ trình duyệt trong LAN (v1.x) phục vụ web UI từ **chính origin này**. Một tệp
HTML mở inline khi ấy chạy script cùng origin với ứng dụng kế toán, đọc được
phiên của người đang xem. Buộc tải về cắt hẳn đường đó, và cái giá phải trả chỉ
là client tự dựng khung xem trước từ luồng byte nó nhận được.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated, Final
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, AuthorizedRequest, SessionFactory, require_permission
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.attachments_schemas import AttachmentListResponse, AttachmentResponse
from ket.kernel.attachments import service, storage
from ket.kernel.attachments.models import Attachment
from ket.kernel.errors import (
    AttachmentBranchRequiredError,
    AttachmentContentMissingError,
    AttachmentNotFoundError,
    AttachmentStorageNotConfiguredError,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.settings import Settings

logger = structlog.get_logger(__name__)

ATTACHMENTS_PREFIX: Final[str] = "/api/v1/attachments"
"""Tiền tố của nhóm, khai ở đây vì `main.py` cần nó để đặt trần thân request
riêng cho đường tải tệp lên. Một hằng số chứ hai chuỗi giống nhau: hai chuỗi sẽ
lệch nhau đúng vào lần đổi đường dẫn, và triệu chứng là trần 1 MiB âm thầm áp
lại cho đính kèm."""

router = APIRouter(prefix=ATTACHMENTS_PREFIX, tags=["attachments"])

ATTACHMENT_VIEW = permission_code(SYSTEM_MODULE, "attachment", Action.VIEW)
ATTACHMENT_CREATE = permission_code(SYSTEM_MODULE, "attachment", Action.CREATE)
ATTACHMENT_DELETE = permission_code(SYSTEM_MODULE, "attachment", Action.DELETE)

AttachmentViewer = Annotated[AuthorizedRequest, Depends(require_permission(ATTACHMENT_VIEW))]
AttachmentAuthor = Annotated[AuthorizedRequest, Depends(require_permission(ATTACHMENT_CREATE))]
AttachmentRemover = Annotated[AuthorizedRequest, Depends(require_permission(ATTACHMENT_DELETE))]

ATTACH_ROUTE: Final[str] = "POST /api/v1/attachments"
AttachKey = Annotated[str, Depends(idempotency_key_dependency(ATTACH_ROUTE))]

DEFAULT_MEDIA_TYPE: Final[str] = "application/octet-stream"

ENTITY_TYPE_PATTERN: Final[str] = r"^[a-z_][a-z0-9_]*$"
"""Tên bảng của bản ghi chủ — cùng quy ước với `audit_log.entity_type`."""

ENTITY_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_.:-]+$"
"""Khóa chính dạng chuỗi: số nguyên, UUID hay mã có dấu gạch. Đủ rộng cho mọi
loại khóa của phase sau, đủ hẹp để không có khoảng trắng hay ký tự điều khiển
lọt vào một cột sẽ được in ra báo cáo."""

EntityType = Annotated[str, Form(max_length=150, pattern=ENTITY_TYPE_PATTERN)]
EntityId = Annotated[str, Form(max_length=200, pattern=ENTITY_ID_PATTERN)]

_MEDIA_TYPE_MAX_LENGTH: Final[int] = 150


def _storage_root(settings: Settings) -> Path:
    """Thư mục kho tệp, hoặc lỗi nêu đúng cách bật."""
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp đính kèm (KET_ATTACHMENTS_DIR)"
        )
    return settings.attachments_dir


def _sanitized_media_type(declared: str | None) -> str:
    """Kiểu nội dung do client khai, sau khi lọc.

    Giá trị này đi thẳng vào header của lượt tải về, nên nó không được mang ký
    tự xuống dòng (chèn header) hay dài vô hạn. Không nhận diện được thì lùi về
    `application/octet-stream` thay vì từ chối lượt tải lên: kiểu nội dung là
    thông tin phụ trợ cho màn hình, còn bản thân tệp thì người dùng cần lưu.
    """
    if declared is None:
        return DEFAULT_MEDIA_TYPE
    value = declared.strip().lower()
    if not value or len(value) > _MEDIA_TYPE_MAX_LENGTH:
        return DEFAULT_MEDIA_TYPE
    kind, separator, subtype = value.partition("/")
    if not separator or not kind or not subtype:
        return DEFAULT_MEDIA_TYPE
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.+-*")
    if not all(character in allowed for character in kind + subtype):
        return DEFAULT_MEDIA_TYPE
    return value


def _safe_file_name(raw: str | None) -> str:
    """Tên hiển thị của tệp: chỉ phần cuối, không đường dẫn, không rỗng.

    Cắt mọi thành phần thư mục vì một số trình duyệt (và mọi client tự viết) gửi
    nguyên đường dẫn đầy đủ. Tên này **không** dùng dựng đường dẫn trên đĩa —
    đường dẫn sinh từ hash — nhưng nó đi vào header `Content-Disposition`, và
    một tên chứa dấu gạch chéo ở đó là thứ trình duyệt diễn giải mỗi bên một
    kiểu.
    """
    candidate = (raw or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(character for character in candidate if character.isprintable())
    return cleaned[:255] or "tep-dinh-kem"


def _content_disposition(file_name: str) -> str:
    """Header buộc tải về, mang được tên tiếng Việt.

    Hai dạng tên trong cùng một header là cố ý: `filename=` ASCII cho client cũ,
    `filename*=UTF-8''…` cho tên có dấu. Bỏ dạng thứ hai thì "Hợp đồng số 12.pdf"
    tải về thành một chuỗi ký tự hỏng.
    """
    # Bỏ `"` và `\` khỏi dạng ASCII: cả hai đều là ký tự **cấu trúc** của giá
    # trị trong ngoặc kép, nên một tên tệp chứa chúng sẽ cắt header thành hai
    # tham số mà trình duyệt mỗi bên đọc một kiểu. Dạng `filename*` bên dưới
    # mang tên đầy đủ nên không mất thông tin gì.
    ascii_fallback = (
        file_name.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
        or "attachment"
    )
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


@router.post(
    "",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    # `200` khai tường minh: lần **gửi lại** cùng khóa idempotency trả về chính
    # tệp đã đính và không tạo gì mới, và mã trạng thái là chỗ duy nhất client
    # biết được điều đó. Không khai thì đặc tả OpenAPI — và type sinh cho client
    # từ nó — chỉ biết `201`, tức là nhánh hay gặp thứ hai của endpoint này vắng
    # mặt trong hợp đồng.
    responses={200: {"model": AttachmentResponse, "description": "Lần gửi lại: tệp đã đính"}},
)
def upload_attachment(
    authorized: AttachmentAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AttachKey,
    response: Response,
    entity_type: EntityType,
    entity_id: EntityId,
    file: Annotated[UploadFile, File()],
) -> AttachmentResponse:
    """Đính một tệp vào một bản ghi — **thực hiện đúng một lần** (FR-NFR-004).

    Tệp ghi xuống đĩa **trước** khi mở transaction, và phải như vậy: vân tay
    idempotency của lượt này là hash nội dung, nên không có cách nào biết hai
    lần gửi có phải cùng một tệp trước khi đọc hết tệp. Hệ quả là một lượt gửi
    lại để lại một tệp trùng trên đĩa — nhưng kho định địa chỉ theo nội dung nên
    "trùng" ở đây nghĩa là **cùng một tệp**, không tốn thêm byte nào.

    Chi nhánh của tệp = chi nhánh đang thao tác của người tải lên, không phải
    tham số client gửi. Đó là thứ quyết định ai còn thấy tệp này sau đó.
    """
    root = _storage_root(settings)
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise AttachmentBranchRequiredError(
            "Chọn chi nhánh đang thao tác (header X-Branch) trước khi đính kèm tệp"
        )

    stored = storage.store_stream(
        root,
        authorized.scope.dataset_schema,
        file.file,
        max_bytes=settings.attachment_max_bytes,
    )
    file_name = _safe_file_name(file.filename)
    media_type = _sanitized_media_type(file.content_type)

    def work(session: Session) -> tuple[AttachmentResponse, IdempotentRef]:
        attachment = service.attach(
            session,
            entity_type=entity_type,
            entity_id=entity_id,
            content_hash=stored.content_hash,
            byte_size=stored.byte_size,
            media_type=media_type,
            file_name=file_name,
            branch_id=branch_id,
            user_id=authorized.scope.user_id,
        )
        return AttachmentResponse.of(attachment), IdempotentRef(
            result_type=Attachment.__tablename__, result_id=str(attachment.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> AttachmentResponse:
        attachment = session.get(Attachment, int(ref.result_id))
        if attachment is None:
            raise AttachmentNotFoundError(
                "Tệp đính kèm của lần thực hiện trước không còn tồn tại",
                attachment=ref.result_id,
            )
        return AttachmentResponse.of(attachment)

    result, created = execute_once(
        factory,
        authorized.scope,
        route_key=ATTACH_ROUTE,
        key=idempotency_key,
        # Vân tay gồm cả hash nội dung: dùng lại một khóa cho **tệp khác** là lỗi
        # client và phải nổ ra, chứ không được im lặng trả về tệp cũ.
        fingerprint=fingerprint_of(f"{entity_type}|{entity_id}|{stored.content_hash}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.get("", response_model=AttachmentListResponse)
def list_attachments(
    authorized: AttachmentViewer,
    factory: SessionFactory,
    entity_type: Annotated[str, Query(max_length=150, pattern=ENTITY_TYPE_PATTERN)],
    entity_id: Annotated[str, Query(max_length=200, pattern=ENTITY_ID_PATTERN)],
) -> AttachmentListResponse:
    """Tệp đang đính vào một bản ghi."""
    with unit_of_work(factory, authorized.scope) as session:
        items = service.list_for_entity(session, entity_type=entity_type, entity_id=entity_id)
        return AttachmentListResponse(items=[AttachmentResponse.of(item) for item in items])


@router.get("/{attachment_id}", response_model=AttachmentResponse)
def get_attachment(
    attachment_id: int, authorized: AttachmentViewer, factory: SessionFactory
) -> AttachmentResponse:
    """Metadata của một tệp."""
    with unit_of_work(factory, authorized.scope) as session:
        return AttachmentResponse.of(service.get_attachment(session, attachment_id))


@router.get(
    "/{attachment_id}/content",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "Nội dung tệp"}},
)
def download_attachment(
    attachment_id: int,
    authorized: AttachmentViewer,
    factory: SessionFactory,
    settings: AppSettings,
) -> StreamingResponse:
    """Tải nội dung tệp về.

    Đọc metadata trong transaction rồi **đóng** nó trước khi phát luồng byte:
    một lượt tải 20 MB qua Wi-Fi văn phòng kéo dài hàng chục giây, và giữ một
    transaction mở suốt thời gian đó là giữ một connection của pool cho một việc
    không còn chạm tới cơ sở dữ liệu.
    """
    root = _storage_root(settings)
    with unit_of_work(factory, authorized.scope) as session:
        attachment = service.get_attachment(session, attachment_id)
        content_hash = attachment.content_hash
        file_name = attachment.file_name
        media_type = attachment.media_type

    path = storage.blob_path(root, authorized.scope.dataset_schema, content_hash)
    if not path.is_file():
        logger.error(
            "attachment.content_missing",
            attachment_id=attachment_id,
            content_hash=content_hash,
            path=str(path),
        )
        raise AttachmentContentMissingError(
            "Tệp có trong danh sách nhưng không đọc được trên máy chủ",
            attachment=attachment_id,
        )

    return StreamingResponse(
        storage.iter_blob(root, authorized.scope.dataset_schema, content_hash),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(file_name),
            # Độ dài lấy từ **tệp thật**, không từ cột `byte_size`. Hai giá trị
            # này chỉ lệch nhau khi kho tệp đã hỏng (khôi phục dở dang, ổ lỗi) —
            # và đúng lúc đó, khai theo metadata làm client treo chờ những byte
            # không bao giờ tới, hoặc nhận một tệp cụt mà tưởng là đủ.
            "Content-Length": str(path.stat().st_size),
            # Không đoán kiểu nội dung từ byte đầu tệp: kiểu ta khai là kiểu
            # client khai lúc tải lên, và trình duyệt tự đoán lại sẽ dựng ra một
            # đường mở inline mà header trên vừa đóng.
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{attachment_id}", response_model=AttachmentResponse)
def detach_attachment(
    attachment_id: int, authorized: AttachmentRemover, factory: SessionFactory
) -> AttachmentResponse:
    """Gỡ tệp khỏi bản ghi chủ.

    Tệp trên đĩa **không** bị xóa: bản ghi khác có thể đang trỏ vào cùng nội
    dung, và một thao tác xóa không rollback được cùng transaction của DB là
    thao tác sẽ có ngày xóa nhầm. Dọn tệp thật sự mồ côi là việc của lượt quét
    đối chiếu ở phase 11.
    """
    with unit_of_work(factory, authorized.scope) as session:
        attachment = service.detach(
            session, attachment_id=attachment_id, user_id=authorized.scope.user_id
        )
        return AttachmentResponse.of(attachment)
