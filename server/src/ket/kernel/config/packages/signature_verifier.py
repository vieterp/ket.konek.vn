"""Verify chữ ký Ed25519 + checksum của một gói cấu hình `.zip` (RT-07).

Hai lớp, chạy theo đúng thứ tự này:

1. **Chữ ký** — `manifest.sig` phải là chữ ký Ed25519 hợp lệ của **đúng byte
   thô** của `manifest.json`, ký bằng một trong các khóa ghim ở
   `publisher_keys.py`. Không parse `manifest.json` trước khi verify: nếu
   verify chạy sau khi đã tin nội dung, một trường JSON dựng khéo có thể đổi
   hành vi kiểm tra trước khi chữ ký kịp bị từ chối.
2. **Checksum từng tệp** — sau khi chữ ký qua, `manifest.json` mới được đọc để
   lấy `sha256` kỳ vọng của bốn tệp dữ liệu; tệp nào trong `.zip` không khớp
   hash đã ký là bị sửa **sau** khi ký — từ chối cả gói.

Không có "verify một phần": một tệp sai là toàn bộ gói bị từ chối, không phụ
thuộc tệp đó có được đọc tới hay không ở bước nạp dữ liệu tiếp theo.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ket.kernel.config.packages.loader import OPTIONAL_DATA_FILES, REQUIRED_DATA_FILES
from ket.kernel.errors import ConfigPackageSignatureInvalidError


@dataclass(frozen=True)
class VerifiedManifest:
    """`manifest.json` sau khi chữ ký đã qua — nội dung vẫn cần đối chiếu checksum."""

    package_code: str
    package_version: int
    file_sha256: Mapping[str, str]
    """Tên tệp → sha256 hex kỳ vọng: đủ bốn khóa của `REQUIRED_DATA_FILES`,
    cộng khóa của tệp tùy chọn (`statements.json`) **nếu** gói mang tệp đó —
    một tệp có mặt trong `.zip` mà không có hash đã ký là nội dung không ai
    ký, bị từ chối ở `verify_file_checksums`."""


def _fail(reason: str, **details: str | int | None) -> ConfigPackageSignatureInvalidError:
    return ConfigPackageSignatureInvalidError(reason, **details)


def _signature_matches_any_key(
    manifest_bytes: bytes, signature_bytes: bytes, public_keys: Sequence[Ed25519PublicKey]
) -> bool:
    for key in public_keys:
        try:
            key.verify(signature_bytes, manifest_bytes)
        except InvalidSignature:
            continue
        return True
    return False


def verify_signature(
    *, manifest_bytes: bytes, signature_bytes: bytes, public_keys: Sequence[Ed25519PublicKey]
) -> VerifiedManifest:
    """Verify chữ ký rồi kiểu hóa `manifest.json`. Ném khi chữ ký sai hoặc
    nội dung manifest không đúng hợp đồng — cả hai đều là "gói không đáng tin".
    """
    if not public_keys:
        raise _fail("Không có khóa publisher nào được ghim để verify")
    if not _signature_matches_any_key(manifest_bytes, signature_bytes, public_keys):
        raise _fail("Chữ ký manifest không khớp bất kỳ khóa publisher nào đã ghim")

    try:
        raw = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(f"manifest.json không phải JSON hợp lệ: {error}") from error
    if not isinstance(raw, Mapping):
        raise _fail("manifest.json phải là một object JSON")

    package = raw.get("package")
    if not isinstance(package, Mapping):
        raise _fail("manifest.json thiếu object `package`")
    code = package.get("code")
    version = package.get("version")
    if not isinstance(code, str) or not code.strip():
        raise _fail("manifest.json thiếu `package.code`")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _fail("manifest.json thiếu `package.version` (số nguyên dương)")

    files = raw.get("files")
    if not isinstance(files, Mapping):
        raise _fail("manifest.json thiếu object `files`")
    file_sha256: dict[str, str] = {}
    for name in REQUIRED_DATA_FILES:
        digest = files.get(name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise _fail(f"manifest.json thiếu sha256 hợp lệ cho tệp {name}", file=name)
        file_sha256[name] = digest.lower()
    for name in OPTIONAL_DATA_FILES:
        digest = files.get(name)
        if digest is None:
            continue
        if not isinstance(digest, str) or len(digest) != 64:
            raise _fail(f"manifest.json mang sha256 không hợp lệ cho tệp {name}", file=name)
        file_sha256[name] = digest.lower()

    return VerifiedManifest(
        package_code=code.strip(), package_version=version, file_sha256=file_sha256
    )


def verify_file_checksums(
    verified_manifest: VerifiedManifest, file_bytes: Mapping[str, bytes]
) -> None:
    """Đối chiếu sha256 thật của từng tệp dữ liệu với con số đã ký.

    Kiểm **hai chiều**: tệp nào có hash đã ký thì phải có mặt và khớp; tệp nào
    có mặt mà không có hash đã ký là nội dung ngoài chữ ký — từ chối. Nhờ chiều
    thứ hai, thêm tệp tùy chọn (`statements.json`) không mở ra đường nhét nội
    dung không ký vào gói.
    """
    for name in verified_manifest.file_sha256:
        content = file_bytes.get(name)
        if content is None:
            raise _fail(f"Thiếu tệp {name} trong gói", file=name)
        actual = hashlib.sha256(content).hexdigest()
        expected = verified_manifest.file_sha256[name]
        if actual != expected:
            raise _fail(
                f"Tệp {name} không khớp checksum đã ký — gói đã bị sửa sau khi ký",
                file=name,
                expected=expected,
                actual=actual,
            )
    unsigned = sorted(file_bytes.keys() - verified_manifest.file_sha256.keys())
    if unsigned:
        raise _fail(
            "Gói chứa tệp dữ liệu không có checksum trong manifest đã ký",
            files=", ".join(unsigned),
        )
