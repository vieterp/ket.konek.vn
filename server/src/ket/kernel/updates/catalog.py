"""Kho gói cập nhật client mà app server tự phục vụ (LD-05, FR-NFR-054).

Bản cài chạy trong LAN **không có internet** (LD-01), nên máy trạm không thể đi
hỏi GitHub Releases hay một CDN nào. App server là nơi duy nhất mọi máy trạm
đều với tới được, nên nó cũng là nơi giữ gói cập nhật.

**Danh mục là một tệp `index.json` do lệnh `publish-update` sinh, không phải kết
quả quét thư mục.** Đây là điểm dễ làm sai nhất: suy phiên bản từ tên tệp nghe
tiện hơn, nhưng khi ấy một tệp đặt sai tên biến thành "cả văn phòng không cập
nhật được" mà không có gì báo — và người triển khai chỉ phát hiện ra khi cần
bản vá gấp. Có danh mục thì lệnh publish là chỗ duy nhất phải đúng, và nó kiểm
được ngay lúc chạy.

Danh mục cũng là **danh sách trắng** của endpoint tải: chỉ tệp có tên trong đó
mới phục vụ được. Chặt hơn hẳn việc lọc chuỗi đường dẫn, vì nó không dựa vào
mình có nghĩ ra đủ cách viết `..` hay không.

Module này **không** biết FastAPI (C1): lệnh CLI ở `ket.admin` và endpoint ở
`ket.api` cùng gọi nó.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ket.kernel.versions import Version, parse_version

CATALOG_FILE_NAME: Final[str] = "index.json"

CATALOG_SCHEMA: Final[int] = 1
"""Phiên bản khuôn của `index.json`. Đọc phải kiểm — một danh mục do bản server
khác ghi mà mình đọc bằng luật cũ là cách âm thầm phục vụ sai gói."""

SUPPORTED_TARGETS: Final[frozenset[str]] = frozenset({"darwin", "windows", "linux"})
"""Tên nền tảng theo đúng từ vựng của updater Tauri (`{{target}}` trong endpoint).

Tập đóng để lệnh publish bắt được lỗi gõ **lúc đẩy gói lên**, chứ không phải để
máy trạm im lặng không bao giờ khớp. `linux` có mặt dù v1 chỉ giao Windows +
macOS (LD-02): chi phí bằng không, và bỏ nó ra không ngăn được gì.
"""

SUPPORTED_ARCHS: Final[frozenset[str]] = frozenset({"x86_64", "aarch64", "i686", "armv7"})


@dataclass(frozen=True, slots=True)
class Release:
    """Một gói cập nhật đã đẩy lên, cho đúng một cặp (nền tảng, kiến trúc)."""

    version: str
    target: str
    arch: str
    file_name: str
    signature: str
    """Nội dung tệp `.sig` do `tauri build` sinh — updater đối chiếu với khóa
    công khai ghim trong `tauri.conf.json`. Lưu **nội dung** chứ không đường dẫn:
    manifest phải trả thẳng chuỗi này, và một tệp `.sig` lạc mất sau khi copy sẽ
    làm mọi máy trạm từ chối bản cập nhật."""
    pub_date: str
    notes: str

    @property
    def parsed_version(self) -> Version | None:
        return parse_version(self.version)


def _catalog_path(updates_dir: Path) -> Path:
    return updates_dir / CATALOG_FILE_NAME


def _is_bare_file_name(name: str) -> bool:
    """Tên tệp trần: không thư mục, không `..`, không tuyệt đối.

    Kiểm ở **cả** lúc ghi lẫn lúc đọc. Lúc ghi để lệnh publish từ chối sớm; lúc
    đọc vì `index.json` là một tệp trên đĩa và có người sửa tay được — một mục
    `"file_name": "../../etc/passwd"` không được phép trở thành một đường tải.
    """
    return bool(name) and name == Path(name).name and name not in {".", ".."}


class CatalogUnreadableError(Exception):
    """Danh mục có mặt nhưng không đọc được: JSON hỏng, thiếu khóa, sai kiểu,
    schema lạ, tên tệp không hợp lệ, hoặc không có quyền đọc.

    Lớp riêng chứ không `ValueError` trần: hai chỗ gọi cần hai hành xử **ngược
    nhau**, và phân biệt chúng bằng kiểu là cách duy nhất không phải bắt
    `Exception` rồi đoán.

    * `publish-update` (người triển khai đang đứng đó) — phải nổ, to và rõ.
    * Endpoint (máy trạm hỏi mỗi giờ) — phải trả `204`/`404` và ghi log. Một bản
      cài có danh mục hỏng vẫn phải **ghi sổ được**; đổi một lỗi đọc tệp thành
      `500` trên mọi máy trạm là biến một sự cố cập nhật thành một sự cố kế toán.
    """


def load_releases(updates_dir: Path | None) -> tuple[Release, ...]:
    """Đọc danh mục. Thiếu thư mục, thiếu tệp, hay danh mục rỗng → `()`.

    **Không ném khi chưa có gì.** Một bản cài chưa đẩy gói nào lên vẫn phải chạy
    bình thường; endpoint sẽ trả `204` và máy trạm hiểu là "chưa có bản mới".
    Hướng hỏng ngược lại — lỗi mỗi lần kiểm — là tiếng ồn trên mọi máy trạm,
    đúng loại tiếng ồn che mất lỗi thật.

    Danh mục **hỏng** thì khác: nó là dấu hiệu ai đó sửa tay hỏng hoặc đĩa lỗi,
    và im lặng bỏ qua sẽ làm bản vá gấp không tới được máy nào. Ném
    `CatalogUnreadableError` để chỗ gọi tự chọn hướng hỏng.
    """
    if updates_dir is None:
        return ()
    path = _catalog_path(updates_dir)
    if not path.is_file():
        return ()

    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogUnreadableError(f"Không đọc được danh mục cập nhật {path}: {error}") from error
    if not isinstance(raw, dict):
        raise CatalogUnreadableError(f"Danh mục cập nhật {path} không phải một đối tượng JSON")
    schema = raw.get("schema")
    if schema != CATALOG_SCHEMA:
        raise CatalogUnreadableError(
            f"Danh mục cập nhật {path} khai schema {schema!r}, bản server này đọc {CATALOG_SCHEMA}"
        )

    entries = raw.get("releases", [])
    if not isinstance(entries, list):
        raise CatalogUnreadableError(f"Danh mục cập nhật {path}: `releases` phải là một danh sách")

    releases: list[Release] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CatalogUnreadableError(f"Danh mục cập nhật {path} có mục không phải đối tượng")
        try:
            release = Release(
                version=str(entry["version"]),
                target=str(entry["target"]),
                arch=str(entry["arch"]),
                file_name=str(entry["file_name"]),
                signature=str(entry["signature"]),
                pub_date=str(entry["pub_date"]),
                notes=str(entry.get("notes", "")),
            )
        except KeyError as error:
            raise CatalogUnreadableError(
                f"Danh mục cập nhật {path} thiếu khóa {error} ở một mục"
            ) from error
        if not _is_bare_file_name(release.file_name):
            raise CatalogUnreadableError(
                f"Danh mục cập nhật {path} có tên tệp không hợp lệ: {release.file_name!r}"
            )
        releases.append(release)
    return tuple(releases)


def latest_newer_than(
    releases: tuple[Release, ...], *, target: str, arch: str, current: Version
) -> Release | None:
    """Bản mới nhất cho đúng cặp (nền tảng, kiến trúc), và **chỉ khi** mới hơn.

    Bản có số phiên bản không đọc được bị bỏ qua thay vì so sánh mò: nó vào
    danh mục qua lệnh publish (đã kiểm khuôn), nên nếu xuất hiện ở đây thì danh
    mục đã bị sửa tay — và phục vụ một gói mình không hiểu số hiệu còn tệ hơn là
    không phục vụ gì.
    """
    best: Release | None = None
    best_version: Version | None = None
    for release in releases:
        if release.target != target or release.arch != arch:
            continue
        parsed = release.parsed_version
        if parsed is None or parsed <= current:
            continue
        if best_version is None or parsed > best_version:
            best, best_version = release, parsed
    return best


def find_release(
    releases: tuple[Release, ...], *, target: str, arch: str, file_name: str
) -> Release | None:
    """Tra gói theo **cả ba** khóa — danh sách trắng của endpoint tải.

    Khóa gồm nền tảng và kiến trúc chứ không chỉ tên tệp: hai nền tảng hoàn toàn
    có thể cùng đặt tên gói giống nhau (`Ket_0.9.0.tar.gz`), và khi ấy tra theo
    mỗi tên sẽ trả về bản đầu tiên khớp — tức là máy Windows tải về một binary
    macOS. Nó tải xong, chữ ký khớp (cùng khóa ký), rồi cài một thứ không chạy.
    """
    for release in releases:
        if release.target == target and release.arch == arch and release.file_name == file_name:
            return release
    return None


def package_path(updates_dir: Path, release: Release) -> Path:
    """Đường dẫn gói trên đĩa — **luôn** dựng qua hàm này.

    Xếp theo `{target}/{arch}/` để hai nền tảng cùng tên tệp không đè nhau. Các
    đoạn đường dẫn lấy từ `Release` đã qua kiểm lúc đọc danh mục (`target`/`arch`
    thuộc tập đóng, `file_name` là tên trần), nên không phép ghép nào ở đây tạo
    ra được đường đi ra ngoài `updates_dir`.
    """
    return updates_dir / release.target / release.arch / release.file_name


def publish(
    updates_dir: Path,
    *,
    package: Path,
    version: str,
    target: str,
    arch: str,
    pub_date: str,
    notes: str = "",
) -> Release:
    """Đưa một gói đã dựng vào kho và ghi lại danh mục.

    Kiểm mọi thứ **trước** khi chạm đĩa: gõ sai `--target` rồi mới phát hiện sau
    khi đã copy 80MB là một kho có rác không ai dọn.

    Bản cùng (phiên bản, nền tảng, kiến trúc) đã có thì **thay thế**, không nhân
    đôi: đẩy lại sau khi dựng lại là việc bình thường của người triển khai.
    """
    if parse_version(version) is None:
        raise ValueError(f"Phiên bản {version!r} không đúng khuôn MAJOR.MINOR.PATCH")
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Nền tảng {target!r} không thuộc {sorted(SUPPORTED_TARGETS)}")
    if arch not in SUPPORTED_ARCHS:
        raise ValueError(f"Kiến trúc {arch!r} không thuộc {sorted(SUPPORTED_ARCHS)}")
    if not package.is_file():
        raise ValueError(f"Không thấy gói cập nhật: {package}")
    if not _is_bare_file_name(package.name):
        raise ValueError(f"Tên gói không hợp lệ: {package.name!r}")

    signature_path = package.with_name(package.name + ".sig")
    if not signature_path.is_file():
        raise ValueError(
            f"Không thấy chữ ký {signature_path}. `tauri build` sinh nó cạnh gói khi "
            "`createUpdaterArtifacts` bật và biến môi trường khóa ký có mặt — thiếu chữ ký "
            "thì mọi máy trạm sẽ từ chối bản cập nhật này."
        )
    signature = signature_path.read_text("utf-8").strip()
    if not signature:
        raise ValueError(f"Chữ ký {signature_path} rỗng")

    release = Release(
        version=version,
        target=target,
        arch=arch,
        file_name=package.name,
        signature=signature,
        pub_date=pub_date,
        notes=notes,
    )

    destination = package_path(updates_dir, release)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if package.resolve() != destination.resolve():
        shutil.copy2(package, destination)

    existing = [
        item
        for item in load_releases(updates_dir)
        if not (item.version == version and item.target == target and item.arch == arch)
    ]
    _write_releases(updates_dir, (*existing, release))
    return release


def _write_releases(updates_dir: Path, releases: tuple[Release, ...]) -> None:
    payload = {
        "schema": CATALOG_SCHEMA,
        "releases": [
            {
                "version": item.version,
                "target": item.target,
                "arch": item.arch,
                "file_name": item.file_name,
                "signature": item.signature,
                "pub_date": item.pub_date,
                "notes": item.notes,
            }
            for item in releases
        ],
    }
    path = _catalog_path(updates_dir)
    # Ghi qua tệp tạm rồi đổi tên: một tiến trình bị ngắt giữa chừng không được
    # để lại một `index.json` cụt — đó là tệp quyết định cả văn phòng có nhận
    # được bản vá hay không.
    # Tên tệp tạm mang PID: hai lệnh `publish-update` chạy cùng lúc (script
    # triển khai đẩy cả Windows lẫn macOS song song) mà dùng chung một tên tạm
    # thì lệnh này xóa tệp tạm của lệnh kia giữa chừng và cả hai cùng nổ.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
