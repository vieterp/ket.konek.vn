"""Nguồn cập nhật client mà app server tự phục vụ (`/updates`) — bước 18, LD-05.

Hai endpoint, cả hai **ẩn danh** và cả hai **chỉ đọc**:

* `GET /updates/{target}/{arch}/{current_version}` — updater Tauri hỏi "có bản
  nào mới hơn bản tôi đang chạy không". `200` + manifest, hoặc `204` khi không.
* `GET /updates/download/{target}/{arch}/{file_name}` — tải chính gói đó. Khóa gồm
  cả nền tảng vì hai nền tảng có thể cùng đặt tên gói giống nhau.

**Vì sao ẩn danh.** Updater chạy ở tầng Rust của shell, ngoài phiên đăng nhập
của web UI: nó kiểm cập nhật trước cả khi có ai gõ mật khẩu, và đúng lúc cần
nhất — bản client quá cũ tới mức server từ chối mọi lệnh ghi của nó — thì phiên
là thứ nó không có. Bắt xác thực ở đây là khóa đường thoát duy nhất của một máy
trạm đã hỏng.

Đổi lại phải thành thật về những gì đường ẩn danh này lộ ra: **số phiên bản đang
phát hành và các gói cài đặt** của phần mềm kế toán. Không dữ liệu kế toán, không
danh tính, không cấu hình. Trên một bản cài LAN, ai đã ở trong mạng ấy thì cũng
đã có màn hình đăng nhập trước mặt.

**Vì sao không đặt dưới `/api/v1`.** Đây không phải hợp đồng cho web UI mà là
hợp đồng cho **updater Tauri**, và khuôn của nó do Tauri định (`{{target}}`,
`{{arch}}`, `{{current_version}}`) chứ không do ta chọn. Đặt cạnh API nghiệp vụ
sẽ ngụ ý nó cùng luật với chúng — cùng phiên, cùng dataset, cùng đánh phiên bản
— mà không điều nào đúng.

Nhưng nó **vẫn nằm trong đặc tả OpenAPI**, và cố ý: khuôn manifest là loại hợp
đồng vỡ trong im lặng (đổi tên một trường thì updater không báo lỗi, nó chỉ coi
như không có bản mới nào). Cổng `api-types-check` so đặc tả đã commit với mã
hiện tại, nên một thay đổi ở đây buộc phải hiện ra trong diff có người đọc.

Cổng phiên bản client (`schema_version_gate`) không chạm tới nhóm này: nó chỉ
chặn **lệnh ghi**, mà ở đây toàn `GET`. Đó là điều kiện bắt buộc, không phải may
mắn — nếu cổng chặn cả đường này thì một client quá cũ sẽ bị chặn khỏi chính thứ
sửa được việc nó quá cũ.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ket.api.dependencies import AppSettings
from ket.kernel.errors import UpdatePackageNotFoundError
from ket.kernel.updates.catalog import (
    CatalogUnreadableError,
    Release,
    find_release,
    latest_newer_than,
    load_releases,
    package_path,
)
from ket.kernel.versions import parse_version

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/updates", tags=["updates"])

DOWNLOAD_SEGMENT: Final[str] = "download"


class UpdateManifest(BaseModel):
    """Khuôn manifest của updater Tauri v2 — **tên trường do Tauri định**.

    Đổi tên bất kỳ trường nào ở đây là updater bỏ qua bản cập nhật trong im
    lặng: nó không báo lỗi khuôn, nó chỉ coi như không có gì mới.
    """

    version: str
    pub_date: str
    url: str
    signature: str
    notes: str = Field(default="")


def _releases_or_empty(updates_dir: Path | None) -> tuple[Release, ...]:
    """Đọc danh mục, và coi "không đọc được" như "không có gì".

    Đây là chỗ H21 sống hay chết. Một `index.json` hỏng — sửa tay lỗi, đĩa lỗi,
    mất quyền đọc, hay đơn giản là do một bản server mới hơn ghi — mà biến thành
    `500` thì **mọi máy trạm** nhận lỗi ở mỗi lượt kiểm cập nhật. Bản cài vẫn
    ghi sổ được bình thường, nên biến một sự cố cập nhật thành một sự cố kế toán
    là sai hướng hỏng.

    Ghi log mức `error`: người quản trị phải thấy, chỉ là không qua đường máy trạm.
    """
    try:
        return load_releases(updates_dir)
    except CatalogUnreadableError as error:
        logger.error("updates.catalog_unreadable", error=str(error))
        return ()


def _manifest_for(request: Request, release: Release) -> UpdateManifest:
    """Dựng manifest, với `url` tuyệt đối theo **chính địa chỉ máy trạm đã gọi**.

    Lấy từ `request.base_url` chứ không từ một giá trị cấu hình: máy trạm tìm
    tới được server bằng địa chỉ nào thì cũng tải gói về bằng đúng địa chỉ ấy.
    Ghim một host trong cấu hình là đúng thứ làm bản cài LAN hỏng — server tự
    khai `localhost` rồi mọi máy trạm đi tải từ chính nó.
    """
    base = str(request.base_url).rstrip("/")
    # `quote` từng đoạn: tên gói có `#` thì phần sau nó thành fragment và
    # updater tải về một URL cụt (đo được: `404` thay vì `200`). Tên artifact
    # của Tauri hiện không có ký tự lạ, nhưng nó do bộ dựng đặt chứ không do ta.
    path = "/".join(
        quote(segment, safe="") for segment in (release.target, release.arch, release.file_name)
    )
    return UpdateManifest(
        version=release.version,
        pub_date=release.pub_date,
        url=f"{base}/updates/{DOWNLOAD_SEGMENT}/{path}",
        signature=release.signature,
        notes=release.notes,
    )


@router.get(
    "/{target}/{arch}/{current_version}",
    response_model=UpdateManifest,
    responses={204: {"description": "Không có bản nào mới hơn"}},
)
def check_for_update(
    target: str,
    arch: str,
    current_version: str,
    request: Request,
    settings: AppSettings,
) -> UpdateManifest | Response:
    """Trả manifest khi có bản mới hơn; `204 No Content` khi không.

    Mọi lối "không có gì để giao" đều đi về `204`, kể cả khi đường tự cập nhật
    chưa được bật hay `current_version` không đọc được. Updater hiểu đúng một
    tín hiệu đó, và một máy trạm không cập nhật được vẫn phải làm việc bình
    thường — hôm nay nó vẫn ghi sổ được, chỉ là chưa lên bản mới.
    """
    releases = _releases_or_empty(settings.updates_dir)
    current = parse_version(current_version)
    if current is None:
        # Không đọc được thì không so sánh mò. Ghi log vì đây là dấu hiệu một
        # bản client dựng sai chứ không phải chuyện thường ngày.
        logger.warning(
            "updates.unparsable_client_version",
            target=target,
            arch=arch,
            current_version=current_version,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    release = latest_newer_than(releases, target=target, arch=arch, current=current)
    if release is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    logger.info(
        "updates.offered",
        target=target,
        arch=arch,
        current_version=current_version,
        offered_version=release.version,
    )
    return _manifest_for(request, release)


@router.get(f"/{DOWNLOAD_SEGMENT}/{{target}}/{{arch}}/{{file_name}}")
def download_package(target: str, arch: str, file_name: str, settings: AppSettings) -> FileResponse:
    """Phục vụ gói — **chỉ** những tệp có tên trong danh mục.

    Danh sách trắng chứ không lọc chuỗi: đường dẫn duy nhất chạm đĩa được dựng
    từ tên đã nằm sẵn trong `index.json`, nên không có phép ghép nào để `..` hay
    một đường tuyệt đối lọt vào. Lọc chuỗi thì phải nghĩ ra đủ mọi cách viết
    `..`; danh sách trắng thì không cần nghĩ ra cách nào.
    """
    releases = _releases_or_empty(settings.updates_dir)
    release = find_release(releases, target=target, arch=arch, file_name=file_name)
    if release is None or settings.updates_dir is None:
        raise UpdatePackageNotFoundError(f"Không có gói cập nhật {file_name!r} cho {target}/{arch}")

    path = package_path(settings.updates_dir, release)
    if not path.is_file():
        # Có trong danh mục mà không có trên đĩa = kho bị dọn tay. Đáng một dòng
        # log: triệu chứng phía máy trạm chỉ là "cập nhật thất bại".
        logger.error("updates.package_missing_on_disk", file_name=release.file_name, path=str(path))
        raise UpdatePackageNotFoundError(
            f"Gói {file_name!r} có trong danh mục nhưng không trên đĩa"
        )

    return FileResponse(path, media_type="application/octet-stream", filename=release.file_name)
