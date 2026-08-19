"""Ký số + nhập gói `.zip` (RT-07) — `signature_verifier.py` + `importer.py`.

Khóa dùng trong test là khóa **dùng-một-lần**, sinh tại chỗ và tiêm qua tham số
`public_keys` của `import_package`/`verify_signature` — không đụng
`publisher_keys.py`, đúng thiết kế cho khả năng test mà không nới lỏng việc
ghim khóa ở đường sản xuất (đường sản xuất luôn gọi không truyền tham số này).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import ChartOfAccount, ConfigPackage
from ket.kernel.config.packages import signature_verifier
from ket.kernel.config.packages.importer import (
    MANIFEST_FILE,
    MAX_ARCHIVE_BYTES,
    SIGNATURE_FILE,
    import_package,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    ConfigPackageArchiveInvalidError,
    ConfigPackageSignatureInvalidError,
    DuplicateValueError,
)
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

_PACKAGE_JSON = "package.json"
_ACCOUNTS_CSV = "accounts.csv"
_DEFAULT_ACCOUNTS_CSV = "default_accounts.csv"
_CLOSING_PAIRS_CSV = "closing_pairs.csv"


def _sample_files(*, code: str = "IMPORT-TEST-1") -> dict[str, bytes]:
    manifest = (
        f'{{"code": "{code}", "scheme": "TT99", "name": "Gói nhập test", '
        '"name_en": null, "description": null, "legal_reference": null, '
        '"effective_from": "2020-01-01", "effective_to": null, "version": 1}'
    )
    accounts = (
        "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
        "detail_tracking,is_locked\n"
        "111,Tiền mặt,,,0,0,0,,1\n"
        "642,Chi phí quản lý,,,3,0,0,,1\n"
    )
    default_accounts = "document_type,purpose,account_code\n*,cash,111\n"
    closing_pairs = "source_account,target_account,sequence,description\n642,111,1,Test\n"
    return {
        _PACKAGE_JSON: manifest.encode("utf-8"),
        _ACCOUNTS_CSV: accounts.encode("utf-8"),
        _DEFAULT_ACCOUNTS_CSV: default_accounts.encode("utf-8"),
        _CLOSING_PAIRS_CSV: closing_pairs.encode("utf-8"),
    }


def _build_zip(
    files: dict[str, bytes],
    *,
    signer: Ed25519PrivateKey,
    tamper_after_signing: dict[str, bytes] | None = None,
    corrupt_signature: bool = False,
    extra_entries: dict[str, bytes] | None = None,
    omit: frozenset[str] = frozenset(),
    deflate: frozenset[str] = frozenset(),
) -> bytes:
    """`deflate` — tên tệp ghi bằng `ZIP_DEFLATED` thay vì mặc định `ZIP_STORED`
    (không nén). Cần cho `test_member_size_cap_is_enforced_after_decompression`:
    nội dung nén rất tốt (toàn byte giống nhau) giữ TOÀN gói `.zip` nhỏ trong
    khi kích thước SAU GIẢI NÉN của đúng một thành viên phình to — cô lập nhánh
    trần từng thành viên khỏi nhánh trần toàn gói (`len(archive_bytes)`).
    """
    manifest = {
        "package": {"code": "IMPORT-TEST-1", "version": 1},
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    signature = signer.sign(manifest_bytes)
    if corrupt_signature:
        signature = bytes([signature[0] ^ 0xFF]) + signature[1:]

    payload = dict(files)
    if tamper_after_signing:
        payload.update(tamper_after_signing)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in payload.items():
            if name in omit:
                continue
            compression = zipfile.ZIP_DEFLATED if name in deflate else zipfile.ZIP_STORED
            archive.writestr(name, content, compression)
        archive.writestr(MANIFEST_FILE, manifest_bytes)
        archive.writestr(SIGNATURE_FILE, signature)
        for name, content in (extra_entries or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


def test_valid_signed_zip_is_imported(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-VALID")
    archive_bytes = _build_zip(files, signer=signer)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package = import_package(
            session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),)
        )
        assert package.is_builtin is False
        assert package.code == "IMPORT-TEST-VALID"
        accounts = session.scalars(
            select(ChartOfAccount).where(ChartOfAccount.package_id == package.id)
        ).all()
        assert {row.code for row in accounts} == {"111", "642"}


def test_duplicate_package_code_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-DUP")
    archive_bytes = _build_zip(files, signer=signer)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(DuplicateValueError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))


def test_tampered_file_after_signing_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-TAMPER")
    archive_bytes = _build_zip(
        files,
        signer=signer,
        tamper_after_signing={_ACCOUNTS_CSV: files[_ACCOUNTS_CSV] + b"\n999,Ma la,,,0,0,0,,0\n"},
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageSignatureInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))
        package = session.scalar(
            select(ConfigPackage).where(ConfigPackage.code == "IMPORT-TEST-TAMPER")
        )
        assert package is None, "gói bị sửa sau khi ký không được ghi gì xuống DB"


def test_corrupted_signature_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    signer = Ed25519PrivateKey.generate()
    archive_bytes = _build_zip(
        _sample_files(code="IMPORT-TEST-BADSIG"), signer=signer, corrupt_signature=True
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageSignatureInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))


def test_signature_from_an_unpinned_key_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    signer = Ed25519PrivateKey.generate()
    unrelated = Ed25519PrivateKey.generate()
    archive_bytes = _build_zip(_sample_files(code="IMPORT-TEST-UNKNOWNKEY"), signer=signer)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageSignatureInvalidError):
            import_package(
                session, archive_bytes=archive_bytes, public_keys=(unrelated.public_key(),)
            )


def test_garbage_bytes_over_the_cap_are_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Rác không phải `.zip` — chặn ngay ở `len()`, còn nếu trần đó bị bỏ thì
    vẫn bị `BadZipFile` chặn hộ (mơ hồ, không tách được hai nhánh — xem hai
    test dưới đây để chứng minh **đúng** nhánh nào chặn khi input là .zip THẬT).
    """
    archive_bytes = b"0" * (MAX_ARCHIVE_BYTES + 1)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=archive_bytes)


def test_whole_archive_size_cap_is_enforced(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Trần TOÀN gói (`len(archive_bytes) > MAX_ARCHIVE_BYTES`) — sửa sau
    review (M10): dùng `.zip` THẬT SỰ mở được (không nén — `ZIP_STORED`, đúng
    mặc định của `_build_zip`), không phải rác. Nếu bỏ trần này, input dưới
    đây sẽ mở được và đi tiếp (không tự nhiên bị `BadZipFile` chặn hộ như test
    trên) — khác `test_garbage_bytes_over_the_cap_are_rejected`, test này thật
    sự phân biệt được "trần còn" và "trần mất".

    Bơm to hai tệp — mỗi tệp vẫn DƯỚI trần từng-thành-viên, nhưng CỘNG LẠI vượt
    trần toàn gói — để nhánh trần-từng-thành-viên không vô tình bắt hộ.
    """
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-BIGARCHIVE")
    per_file_padding = b"#" * (3 * 1024 * 1024)  # 3 MiB — dưới trần 5 MiB/thành viên
    files[_ACCOUNTS_CSV] = files[_ACCOUNTS_CSV] + b"\n# " + per_file_padding + b"\n"
    files[_CLOSING_PAIRS_CSV] = files[_CLOSING_PAIRS_CSV] + b"\n# " + per_file_padding + b"\n"

    archive_bytes = _build_zip(files, signer=signer)
    assert len(archive_bytes) > MAX_ARCHIVE_BYTES, "gói dựng phải thật sự vượt trần toàn gói"
    assert all(len(content) <= MAX_ARCHIVE_BYTES for content in files.values()), (
        "từng tệp phải dưới trần thành viên — nếu không, test này lẫn với nhánh kia"
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))


def test_member_size_cap_is_enforced_after_decompression(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Trần TỪNG THÀNH VIÊN sau giải nén (`info.file_size`, chống zip bomb) —
    sửa sau review (M10). Ngược pha với test trên: nội dung nén cực tốt (toàn
    byte `0`, `ZIP_DEFLATED`) giữ TOÀN gói `.zip` nhỏ hơn hẳn trần, chỉ đúng
    một thành viên phình to sau giải nén — chỉ nhánh trần-thành-viên bắt được.
    """
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-ZIPBOMB")
    files[_DEFAULT_ACCOUNTS_CSV] = b"0" * (MAX_ARCHIVE_BYTES + 1024)

    archive_bytes = _build_zip(files, signer=signer, deflate=frozenset({_DEFAULT_ACCOUNTS_CSV}))
    assert len(archive_bytes) < MAX_ARCHIVE_BYTES, (
        "toàn gói phải nhỏ — nén tốt, chỉ thành viên phình to sau giải nén"
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))


def test_unexpected_entry_name_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Mô phỏng đường tấn công zip-slip: một mục tên lạ (`../` hoặc thư mục con)
    không nằm trong đúng sáu tên cho phép — bị chặn ở bước đọc cấu trúc, trước
    khi bất kỳ nội dung nào được ghi ra ngoài đúng sáu tên đó.
    """
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-SLIP")
    archive_bytes = _build_zip(
        files,
        signer=signer,
        omit=frozenset({_CLOSING_PAIRS_CSV}),
        extra_entries={"../evil.txt": b"payload"},
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))


def test_verify_signature_returns_typed_manifest() -> None:
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-VERIFY")
    manifest = {
        "package": {"code": "IMPORT-TEST-VERIFY", "version": 1},
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    signature = signer.sign(manifest_bytes)

    verified = signature_verifier.verify_signature(
        manifest_bytes=manifest_bytes,
        signature_bytes=signature,
        public_keys=(signer.public_key(),),
    )
    assert verified.package_code == "IMPORT-TEST-VERIFY"
    assert verified.package_version == 1
    signature_verifier.verify_file_checksums(verified, files)


def test_crc_corrupt_member_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Mục .zip sai CRC phải bị từ chối bằng lỗi nghiệp vụ, không phải BadZipFile thô.

    `ZipFile(...)` mở được vẫn chưa chứng minh đọc được: CRC từng mục chỉ được
    kiểm lúc `read()`. Hỏng byte trong payload SAU khi ký/ghi header giữ nguyên
    CRC cũ trong header → `read()` nổ giữa chừng.
    """
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-CRC")
    archive_bytes = _build_zip(files, signer=signer)
    # accounts.csv ghi ZIP_STORED nên payload nằm verbatim trong .zip — thay một
    # chuỗi bằng rác cùng độ dài làm nội dung lệch khỏi CRC đã ghi ở header.
    original = "Tiền mặt".encode()
    assert archive_bytes.count(original) == 1, "chuỗi mồi phải xuất hiện đúng một lần"
    corrupted = archive_bytes.replace(original, b"X" * len(original))

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=corrupted, public_keys=(signer.public_key(),))
        package = session.scalar(
            select(ConfigPackage).where(ConfigPackage.code == "IMPORT-TEST-CRC")
        )
        assert package is None, "gói hỏng CRC không được ghi gì xuống DB"


def test_non_utf8_member_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tệp không phải UTF-8 hợp lệ bị từ chối fail-closed, không rò UnicodeDecodeError.

    Checksum/chữ ký ký trên **byte** nên gói ký đúng vẫn có thể chứa văn bản
    hỏng — lượt decode là hàng rào riêng, sau verify.
    """
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-UTF8")
    files[_ACCOUNTS_CSV] = b"\xff\xfe\x00\x01kh\xf4ng ph\xe3i utf-8"
    archive_bytes = _build_zip(files, signer=signer)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageArchiveInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))
        package = session.scalar(
            select(ConfigPackage).where(ConfigPackage.code == "IMPORT-TEST-UTF8")
        )
        assert package is None


_STATEMENTS_JSON = (
    b'[{"code": "T01", "name": "Layout test", "statement_kind": "balance_sheet",'
    b' "rows": [{"row_code": "110", "label": "Ti\xe1\xbb\x81n", "formula": "DR(111)"},'
    b' {"row_code": "100", "label": "T\xe1\xbb\x95ng", "formula": "[110]"}]}]'
)


def test_signed_statements_json_is_imported_with_layouts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`statements.json` là mục tùy chọn thứ bảy (lát 5B): có mặt + có checksum
    trong manifest đã ký → layout vào DB cùng transaction với gói."""
    from ket.kernel.config.statements.models import StatementLayout, StatementRow

    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-STMT")
    files["statements.json"] = _STATEMENTS_JSON
    archive_bytes = _build_zip(files, signer=signer)

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        package = import_package(
            session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),)
        )
        layout = session.scalar(
            select(StatementLayout).where(StatementLayout.package_id == package.id)
        )
        assert layout is not None and layout.code == "T01"
        rows = session.scalars(
            select(StatementRow)
            .where(StatementRow.layout_id == layout.id)
            .order_by(StatementRow.display_order)
        ).all()
        assert [row.row_code for row in rows] == ["110", "100"]


def test_statements_json_without_signed_checksum_is_rejected(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Tệp có mặt trong `.zip` mà không có sha256 trong manifest đã ký = nội
    dung không ai ký — kiểm HAI CHIỀU của `verify_file_checksums` phải chặn,
    nếu không mục tùy chọn thành cửa nhét layout không ký vào gói (RT-07)."""
    signer = Ed25519PrivateKey.generate()
    files = _sample_files(code="IMPORT-TEST-STMT-UNSIGNED")
    archive_bytes = _build_zip(
        files, signer=signer, extra_entries={"statements.json": _STATEMENTS_JSON}
    )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ConfigPackageSignatureInvalidError):
            import_package(session, archive_bytes=archive_bytes, public_keys=(signer.public_key(),))
        package = session.scalar(
            select(ConfigPackage).where(ConfigPackage.code == "IMPORT-TEST-STMT-UNSIGNED")
        )
        assert package is None
