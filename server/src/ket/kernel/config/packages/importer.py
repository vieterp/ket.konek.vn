"""Nhập một gói cấu hình đã ký từ `.zip` (RT-07).

Trình tự — mỗi bước là một cổng chặn cứng, hỏng ở bước nào thì **không gì**
được ghi xuống DB (transaction ORM của người gọi vẫn mở, nhưng hàm này chưa
`session.add` gì trước khi qua hết bốn cổng):

1. **Cấu trúc `.zip`** — đúng sáu tên tệp cho phép, không thư mục con, không
   tệp trùng tên, không vượt trần dung lượng (chống zip-slip + zip bomb).
2. **Chữ ký** (`signature_verifier.verify_signature`) — `manifest.sig` khớp
   một khóa đã ghim.
3. **Checksum từng tệp** (`signature_verifier.verify_file_checksums`) — không
   tệp nào bị sửa sau khi ký.
4. **Luật dữ liệu** (`loader.load_package_from_texts`) — cùng bộ kiểm với gói
   dựng sẵn (mã trùng, tài khoản cha thiếu, `detail_tracking` lạ…).

Chỉ đọc đúng sáu tên tệp cố định bằng `ZipFile.read(name)` — không bao giờ ghép
tên mục trong `.zip` vào đường dẫn trên đĩa, nên không có bề mặt zip-slip
(traversal `../`) dù tên mục trong `.zip` có là gì.

**Bất biến (sửa sau review, C1 CRITICAL): nhập ≠ có hiệu lực.** Gói ghi ở đây
luôn `activated_at = NULL` — nó **nằm im**, không TK hay dòng chứng từ nào đọc
tới nó, cho tới khi có người gọi `POST /actions/activate` (`activator.activate`,
mang cổng khóa đổi chế độ kế toán FR-SYS-004 + đòi 2FA). `resolve_package`
(`accounts_provider.py`) chỉ đọc gói đã kích hoạt, nên hàm nhập ở đây **không
cần** — và cố tình **không có** — một cổng scheme-lock riêng: qua bốn cổng trên
chỉ đưa dữ liệu vào bàn, không đưa vào sản xuất.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from collections.abc import Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import (
    ChartOfAccount,
    ClosingAccountPair,
    ConfigPackage,
    DefaultAccount,
)
from ket.kernel.config.packages import signature_verifier
from ket.kernel.config.packages.loader import (
    ACCOUNTS_FILE,
    CLOSING_PAIRS_FILE,
    DEFAULT_ACCOUNTS_FILE,
    PACKAGE_MANIFEST_FILE,
    LoadedPackage,
    load_package_from_texts,
)
from ket.kernel.config.packages.publisher_keys import pinned_public_keys
from ket.kernel.errors import ConfigPackageArchiveInvalidError, DuplicateValueError
from ket.kernel.master_data.tree_path import ROOT_LEVEL, child_path, level_of

MANIFEST_FILE = "manifest.json"
SIGNATURE_FILE = "manifest.sig"

_DATA_FILES: tuple[str, ...] = (
    PACKAGE_MANIFEST_FILE,
    ACCOUNTS_FILE,
    DEFAULT_ACCOUNTS_FILE,
    CLOSING_PAIRS_FILE,
)
_EXPECTED_ENTRIES = frozenset({*_DATA_FILES, MANIFEST_FILE, SIGNATURE_FILE})

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
"""Trần dung lượng gói `.zip` — fail-closed. Gói cấu hình là văn bản (CSV/JSON),
không có lý do chính đáng nào để vượt vài trăm KB; 5 MiB đã rộng gấp nhiều lần
hệ thống tài khoản đầy đủ nhất trong repo (~200 dòng)."""


def _reject(reason: str, **details: str | int | None) -> ConfigPackageArchiveInvalidError:
    raise ConfigPackageArchiveInvalidError(reason, **details)


def _open_validated_zip(archive_bytes: bytes) -> zipfile.ZipFile:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise _reject(f"Gói .zip vượt trần {MAX_ARCHIVE_BYTES} byte", size=len(archive_bytes))
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as error:
        raise _reject(f"Không đọc được .zip: {error}") from error

    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise _reject("Gói .zip chứa tên tệp trùng lặp")
    if set(names) != _EXPECTED_ENTRIES:
        raise _reject(
            "Gói .zip không đúng cấu trúc cho phép — phải đúng sáu tệp của hợp đồng",
            expected=",".join(sorted(_EXPECTED_ENTRIES)),
            found=",".join(sorted(names)),
        )
    for info in infos:
        if info.is_dir():
            raise _reject("Gói .zip không được chứa thư mục con", entry=info.filename)
        if info.file_size > MAX_ARCHIVE_BYTES:
            raise _reject(
                f"Tệp {info.filename} trong .zip vượt trần {MAX_ARCHIVE_BYTES} byte "
                "sau khi giải nén (chống zip bomb)",
                entry=info.filename,
                size=info.file_size,
            )
    return archive


def _read_all(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """Đọc trọn sáu mục — mục hỏng (sai CRC, nén lỗi) là gói bị từ chối.

    `ZipFile(...)` mở được không có nghĩa đọc được: CRC của từng mục chỉ được
    kiểm lúc `read()`. Không bọc ở đây thì `BadZipFile`/`zlib.error` thoát lên
    thành HTTP 500 thô, trái RT-07 ("sai cấu trúc → từ chối cả gói") và
    FR-NFR-050.
    """
    result: dict[str, bytes] = {}
    for name in _EXPECTED_ENTRIES:
        try:
            result[name] = archive.read(name)
        except (zipfile.BadZipFile, zlib.error) as error:
            raise _reject(
                f"Mục {name} trong .zip hỏng, không đọc được: {error}", entry=name
            ) from error
    return result


def _verify_and_load(
    archive_bytes: bytes, *, public_keys: Sequence[Ed25519PublicKey]
) -> LoadedPackage:
    archive = _open_validated_zip(archive_bytes)
    raw = _read_all(archive)

    verified = signature_verifier.verify_signature(
        manifest_bytes=raw[MANIFEST_FILE],
        signature_bytes=raw[SIGNATURE_FILE],
        public_keys=public_keys,
    )
    signature_verifier.verify_file_checksums(verified, {name: raw[name] for name in _DATA_FILES})

    texts: dict[str, str] = {}
    for name in _DATA_FILES:
        try:
            texts[name] = raw[name].decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise _reject(
                f"Tệp {name} trong gói không phải văn bản UTF-8 hợp lệ", entry=name
            ) from error
    return load_package_from_texts(texts)


def _insert_imported_package(session: Session, loaded: LoadedPackage) -> ConfigPackage:
    manifest = loaded.manifest
    existing = session.scalar(select(ConfigPackage).where(ConfigPackage.code == manifest.code))
    if existing is not None:
        raise DuplicateValueError(
            "Đã có gói cấu hình với mã này — không nhập trùng", constraint="config_packages_code"
        )

    package = ConfigPackage(
        code=manifest.code,
        name=manifest.name,
        description=manifest.description,
        scheme=manifest.scheme,
        legal_reference=manifest.legal_reference,
        effective_from=manifest.effective_from,
        effective_to=manifest.effective_to,
        version=manifest.version,
        is_builtin=False,
        # Nằm im tới khi kích hoạt — xem "Bất biến" ở docstring đầu tệp.
        activated_at=None,
        activated_by=None,
    )
    session.add(package)
    session.flush()

    id_by_code: dict[str, int] = {}
    path_by_code: dict[str, str] = {}
    for row in loaded.accounts:
        parent_id = id_by_code.get(row.parent_code) if row.parent_code else None
        parent_path = path_by_code.get(row.parent_code) if row.parent_code else None
        account = ChartOfAccount(
            package_id=package.id,
            code=row.code,
            name=row.name,
            name_en=row.name_en,
            parent_id=parent_id,
            path="0.",  # giá trị tạm — sửa lại ngay dưới đây khi đã có `id`
            level=ROOT_LEVEL,
            balance_nature=row.balance_nature,
            is_summary=row.is_summary,
            is_foreign_currency=row.is_foreign_currency,
            detail_tracking=list(row.detail_tracking) or None,
            is_locked=row.is_locked,
        )
        session.add(account)
        session.flush()
        path = child_path(parent_path, account.id)
        account.path = path
        account.level = level_of(path)
        id_by_code[row.code] = account.id
        path_by_code[row.code] = path

    for default_row in loaded.default_accounts:
        session.add(
            DefaultAccount(
                package_id=package.id,
                document_type=default_row.document_type,
                purpose=default_row.purpose,
                account_code=default_row.account_code,
            )
        )
    for pair_row in loaded.closing_pairs:
        session.add(
            ClosingAccountPair(
                package_id=package.id,
                source_account=pair_row.source_account,
                target_account=pair_row.target_account,
                sequence=pair_row.sequence,
                description=pair_row.description,
            )
        )
    session.flush()
    return package


def import_package(
    session: Session,
    *,
    archive_bytes: bytes,
    public_keys: Sequence[Ed25519PublicKey] | None = None,
) -> ConfigPackage:
    """Verify + nạp + ghi một gói `.zip` đã ký. `is_builtin=False` luôn.

    `public_keys` mặc định là bộ khóa ghim thật (`publisher_keys.pinned_public_keys`);
    tham số tồn tại **chỉ** để test tự ký bằng khóa dùng-một-lần mà không phải
    đụng vào `publisher_keys.py` — production luôn gọi hàm này không truyền
    tham số, tức luôn dùng khóa ghim thật.
    """
    keys = public_keys if public_keys is not None else pinned_public_keys()
    loaded = _verify_and_load(archive_bytes, public_keys=keys)
    return _insert_imported_package(session, loaded)
