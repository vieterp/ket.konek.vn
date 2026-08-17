"""Kho tệp định địa chỉ theo nội dung — không biết gì về cơ sở dữ liệu.

Đường dẫn của một tệp sinh **hoàn toàn** từ SHA-256 của chính nó:

    <gốc>/<schema dataset>/<hash[0:2]>/<hash[2:4]>/<hash>

Ba tính chất theo sau, và cả ba đều là lý do chọn cách này:

* **Không có phép ghép chuỗi nào chạm tới dữ liệu người dùng.** Tên tệp người
  dùng gửi lên chỉ đi vào cột metadata; nó không tham gia dựng đường dẫn, nên
  `../../etc/passwd` hay một tên 300 ký tự tiếng Việt không phải mối lo ở đây.
* **Trùng nội dung = trùng tệp.** Cùng một hợp đồng scan đính vào năm chứng từ
  chiếm một chỗ trên đĩa.
* **Tách theo schema dataset.** Hai doanh nghiệp trong cùng một bản cài không
  dùng chung thư mục, nên sao lưu/khôi phục một dataset (RT-03, per-schema) bốc
  được cả tệp lẫn dữ liệu, và một lần khôi phục nhầm không kéo tệp của doanh
  nghiệp khác sang.

**Không mã hóa ở tầng này** (quyết định lát 2C-5): tệp dựa vào quyền của hệ điều
hành trên thư mục, giống thư mục sao lưu. Bắt buộc mã hóa (RT-03) gắn ở *bản sao
lưu* — thứ rời khỏi máy chủ — chứ không ở kho đang sống, nơi mọi lượt tải về sẽ
phải giải mã và nơi mất khóa app đồng nghĩa mất trắng mười năm chứng từ scan.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import IO, Final

from ket.kernel.datasets.naming import validate_schema_name
from ket.kernel.errors import (
    AttachmentEmptyError,
    AttachmentStorageUnavailableError,
    AttachmentTooLargeError,
)

CHUNK_SIZE: Final[int] = 1024 * 1024
"""Kích thước lô đọc/ghi. 1 MiB đủ lớn để không phải gọi hệ thống liên tục và đủ
nhỏ để một lượt tải lên không ngốn bộ nhớ theo kích thước tệp."""

_TEMP_DIR_NAME: Final[str] = "incoming"


@dataclass(frozen=True)
class StoredBlob:
    """Kết quả một lượt ghi: cái mà bảng metadata cần biết."""

    content_hash: str
    byte_size: int
    deduplicated: bool
    """Tệp đã có sẵn trên đĩa từ trước, lượt này không ghi thêm byte nào."""


def dataset_root(root: Path, dataset_schema: str) -> Path:
    """Thư mục tệp của một dataset. Tên schema được kiểm trước khi ghép."""
    return root / validate_schema_name(dataset_schema)


def blob_path(root: Path, dataset_schema: str, content_hash: str) -> Path:
    """Đường dẫn tuyệt đối của một tệp đã lưu.

    Hai tầng thư mục con hai ký tự: một thư mục phẳng chứa hàng trăm nghìn tệp
    làm chậm hẳn mọi thao tác duyệt trên NTFS lẫn APFS, và người quản trị thì
    vẫn phải mở được thư mục đó bằng tay khi cứu dữ liệu.
    """
    digest = _validated_hash(content_hash)
    return dataset_root(root, dataset_schema) / digest[0:2] / digest[2:4] / digest


def store_stream(
    root: Path,
    dataset_schema: str,
    source: IO[bytes],
    *,
    max_bytes: int,
) -> StoredBlob:
    """Ghi luồng byte vào kho, trả về hash + kích thước.

    Băm **trong lúc ghi**, và chặn trần **trong lúc ghi**: `Content-Length` do
    client khai không phải bằng chứng, nên một lượt tải lên nói dối 1 KB rồi đẩy
    2 GB phải dừng ở đúng byte vượt ngưỡng chứ không phải sau khi đĩa đã đầy.

    Ghi ra tệp tạm **cùng thư mục dataset** rồi `os.replace`: cùng hệ tệp nên
    phép đổi tên là nguyên tử, và người đọc không bao giờ thấy một tệp ghi dở
    mang tên của một hash — thứ sẽ trông y hệt một tệp hợp lệ đã hỏng.
    """
    # Ổ chưa gắn, thư mục sai quyền, đĩa đầy: sự cố **vận hành**, người quản trị
    # sửa được trong một phút. Bọc **toàn bộ** đường ghi chứ không chỉ lúc mở
    # tệp — hỏng có thể rơi vào bất kỳ bước nào (tạo thư mục băm, ghi lô cuối,
    # `fsync`, đổi tên), và mỗi bước để lọt là một đường trả về `500 "lỗi không
    # mong muốn, báo bộ phận hỗ trợ"`, câu che mất đúng thứ cần nói.
    try:
        return _store(root, dataset_schema, source, max_bytes=max_bytes)
    except OSError as error:
        raise AttachmentStorageUnavailableError(
            "Không ghi được vào thư mục tệp đính kèm của máy chủ", reason=error.strerror
        ) from error


def _store(
    root: Path,
    dataset_schema: str,
    source: IO[bytes],
    *,
    max_bytes: int,
) -> StoredBlob:
    """Thân của `store_stream`, tách ra để một `try` bọc trọn đường ghi."""
    target_root = dataset_root(root, dataset_schema)
    staging = target_root / _TEMP_DIR_NAME
    staging.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=staging, suffix=".part")

    digest = sha256()
    size = 0
    temp_path = Path(temp_name)
    moved = False
    try:
        with os.fdopen(handle, "wb") as buffer:
            while chunk := source.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise AttachmentTooLargeError(
                        "Tệp đính kèm vượt quá dung lượng cho phép của bản cài",
                        max_bytes=max_bytes,
                    )
                digest.update(chunk)
                buffer.write(chunk)
            buffer.flush()
            # `fsync` trước khi đổi tên: sau lượt tải lên này, DB sẽ giữ một dòng
            # nói rằng tệp tồn tại. Mất điện giữa hai việc đó mà nội dung còn nằm
            # trong cache của hệ điều hành thì dòng metadata sẽ trỏ vào một tệp
            # rỗng — và không có cách nào biết trước lần tải về đầu tiên.
            os.fsync(buffer.fileno())

        if size == 0:
            raise AttachmentEmptyError("Tệp đính kèm rỗng")

        content_hash = digest.hexdigest()
        destination = blob_path(root, dataset_schema, content_hash)
        if destination.exists():
            # Trùng nội dung. Không ghi đè: tệp đã ở đó có cùng hash nên cùng nội
            # dung, và ghi đè chỉ tạo một khoảng thời gian mà người đang tải về
            # đọc phải tệp dở.
            return StoredBlob(content_hash=content_hash, byte_size=size, deduplicated=True)

        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, destination)
        moved = True
        return StoredBlob(content_hash=content_hash, byte_size=size, deduplicated=False)
    finally:
        # Mọi lối thoát khác — vượt trần, tệp rỗng, luồng đứt giữa chừng — đều
        # phải để lại thư mục sạch. Không dọn thì `incoming/` là chỗ một lượt
        # tải lên hỏng chiếm đĩa mà không bản ghi nào nhắc tới nó.
        if not moved:
            temp_path.unlink(missing_ok=True)


def open_blob(root: Path, dataset_schema: str, content_hash: str) -> IO[bytes]:
    """Mở tệp để đọc. Ném `FileNotFoundError` nếu không có — người gọi xử lý."""
    return blob_path(root, dataset_schema, content_hash).open("rb")


def iter_blob(root: Path, dataset_schema: str, content_hash: str) -> Iterator[bytes]:
    """Đọc tệp theo lô cho phản hồi HTTP dạng luồng."""
    with open_blob(root, dataset_schema, content_hash) as handle:
        while chunk := handle.read(CHUNK_SIZE):
            yield chunk


def _validated_hash(content_hash: str) -> str:
    """Chỉ nhận SHA-256 hex chữ thường.

    Hash đi vào hàm này đến từ cột DB, không từ người dùng — nhưng một dòng dữ
    liệu hỏng (khôi phục dở dang, sửa tay) không được phép trở thành đường ghép
    `..` vào đường dẫn. Kiểm ở đúng chỗ dựng đường dẫn thì không có lối vòng nào.
    """
    if len(content_hash) != 64 or not all(c in "0123456789abcdef" for c in content_hash):
        raise ValueError(f"content_hash không phải SHA-256 hex: {content_hash!r}")
    return content_hash
