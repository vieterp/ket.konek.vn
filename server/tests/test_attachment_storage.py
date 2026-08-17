"""Kho tệp định địa chỉ theo nội dung (FR-NFR-053).

Không cần PostgreSQL: tầng này chỉ biết hệ tệp. Bốn bất biến, và cái giá của
việc mất từng cái đo được:

1. **Đường dẫn sinh từ hash, không từ tên người dùng.** Mất nó là mở đường ghi
   ra ngoài thư mục kho bằng một tên tệp.
2. **Trần dung lượng chặn trong lúc ghi.** Tin `Content-Length` client khai thì
   một lượt tải lên nói dối làm đầy đĩa máy chủ.
3. **Trùng nội dung không ghi thêm byte nào.** Một hợp đồng scan đính vào năm
   chứng từ phải chiếm một chỗ.
4. **Lượt ghi hỏng không để lại rác.** `incoming/` là chỗ duy nhất tệp dở nằm
   lại, và không ai dọn nó bằng tay được vì không bản ghi nào nhắc tới.
"""

from __future__ import annotations

import io
from hashlib import sha256
from pathlib import Path

import pytest

from ket.kernel.attachments import storage
from ket.kernel.errors import (
    AttachmentEmptyError,
    AttachmentStorageUnavailableError,
    AttachmentTooLargeError,
    InvalidSchemaNameError,
)

SCHEMA = "ds_alpha"
CONTENT = "Hợp đồng số 12/2026 — bản scan".encode()
CONTENT_HASH = sha256(CONTENT).hexdigest()

MAX_BYTES = 1024


def _store(root: Path, payload: bytes, *, max_bytes: int = MAX_BYTES) -> storage.StoredBlob:
    return storage.store_stream(root, SCHEMA, io.BytesIO(payload), max_bytes=max_bytes)


def test_content_lands_at_a_path_derived_from_its_hash(tmp_path: Path) -> None:
    stored = _store(tmp_path, CONTENT)

    assert stored.content_hash == CONTENT_HASH
    assert stored.byte_size == len(CONTENT)
    assert not stored.deduplicated

    path = storage.blob_path(tmp_path, SCHEMA, CONTENT_HASH)
    assert path.read_bytes() == CONTENT
    # Hai tầng thư mục con: một thư mục phẳng vài trăm nghìn tệp là thứ người
    # quản trị không mở nổi đúng lúc họ cần cứu dữ liệu.
    assert path.relative_to(tmp_path).parts == (
        SCHEMA,
        CONTENT_HASH[0:2],
        CONTENT_HASH[2:4],
        CONTENT_HASH,
    )


def test_two_datasets_never_share_a_directory(tmp_path: Path) -> None:
    """Cùng nội dung, hai dữ liệu kế toán → hai tệp ở hai nhánh thư mục.

    Không gộp dù nội dung trùng: sao lưu/khôi phục làm **theo từng schema**
    (RT-03), nên một tệp dùng chung sẽ biến mất khỏi doanh nghiệp A khi có người
    khôi phục doanh nghiệp B từ một bản dump cũ hơn.
    """
    _store(tmp_path, CONTENT)
    storage.store_stream(tmp_path, "ds_beta", io.BytesIO(CONTENT), max_bytes=MAX_BYTES)

    alpha = storage.blob_path(tmp_path, SCHEMA, CONTENT_HASH)
    beta = storage.blob_path(tmp_path, "ds_beta", CONTENT_HASH)
    assert alpha != beta
    assert alpha.read_bytes() == beta.read_bytes() == CONTENT


def test_storing_the_same_content_twice_writes_nothing_new(tmp_path: Path) -> None:
    first = _store(tmp_path, CONTENT)
    second = _store(tmp_path, CONTENT)

    assert second.deduplicated and not first.deduplicated
    assert second.content_hash == first.content_hash
    files = [path for path in (tmp_path / SCHEMA).rglob("*") if path.is_file()]
    assert len(files) == 1, "trùng nội dung phải là trùng tệp, không phải hai tệp giống nhau"


def test_a_stream_over_the_limit_is_refused_and_leaves_nothing_behind(tmp_path: Path) -> None:
    oversized = b"x" * (MAX_BYTES + 1)

    with pytest.raises(AttachmentTooLargeError):
        _store(tmp_path, oversized)

    leftovers = [path for path in (tmp_path / SCHEMA).rglob("*") if path.is_file()]
    assert leftovers == [], "lượt ghi hỏng để lại rác trong thư mục dàn dựng"


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    """Tệp 0 byte gần như luôn là chọn nhầm, và nó trông y hệt tệp thật trong danh sách."""
    with pytest.raises(AttachmentEmptyError):
        _store(tmp_path, b"")

    assert [path for path in (tmp_path / SCHEMA).rglob("*") if path.is_file()] == []


def test_a_failing_stream_leaves_nothing_behind(tmp_path: Path) -> None:
    """Luồng đứt giữa lượt tải lên: không có tệp dở nào mang tên một hash.

    `AttachmentStorageUnavailableError` chứ không `OSError` thô: mọi lỗi hệ tệp
    trong đường ghi đều quy về một câu trả lời `503` nói rằng máy chủ không lưu
    được tệp này. Người dùng không phân biệt được "đọc hỏng" với "ghi hỏng", và
    hai mã lỗi cho cùng một hành động kế tiếp (thử lại) chỉ làm loãng."""

    class Broken(io.RawIOBase):
        def read(self, size: int = -1) -> bytes:
            raise OSError("kết nối đứt giữa chừng")

    with pytest.raises(AttachmentStorageUnavailableError):
        storage.store_stream(tmp_path, SCHEMA, Broken(), max_bytes=MAX_BYTES)  # type: ignore[arg-type]

    assert [path for path in (tmp_path / SCHEMA).rglob("*") if path.is_file()] == []


@pytest.mark.parametrize("schema", ["../etc", "ds alpha", "pg_catalog", "public"])
def test_a_dangerous_schema_name_never_becomes_a_path(tmp_path: Path, schema: str) -> None:
    """Tên schema đi vào đường dẫn phải qua đúng bộ luật của `datasets/naming`."""
    with pytest.raises(InvalidSchemaNameError):
        storage.dataset_root(tmp_path, schema)


@pytest.mark.parametrize(
    "content_hash",
    ["../../etc/passwd", "ZZ" + "0" * 62, "abc", CONTENT_HASH.upper()],
)
def test_a_hash_that_is_not_sha256_hex_never_becomes_a_path(
    tmp_path: Path, content_hash: str
) -> None:
    """Một dòng metadata hỏng không được trở thành đường ghép `..` vào đường dẫn.

    `ValueError` chứ không `DomainError`: tới đây nghĩa là dữ liệu trong DB đã
    sai, không phải người dùng gõ sai — nó phải nổ to và vào log.
    """
    with pytest.raises(ValueError, match="SHA-256"):
        storage.blob_path(tmp_path, SCHEMA, content_hash)


def test_reading_back_streams_the_exact_bytes(tmp_path: Path) -> None:
    payload = b"a" * (storage.CHUNK_SIZE + 17)
    stored = _store(tmp_path, payload, max_bytes=len(payload))

    chunks = list(storage.iter_blob(tmp_path, SCHEMA, stored.content_hash))

    assert b"".join(chunks) == payload
    assert len(chunks) > 1, "tệp lớn hơn một lô phải đọc theo lô, không nạp cả vào bộ nhớ"
