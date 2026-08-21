"""Đọc và kiểm một thư mục dữ liệu gói cấu hình (hợp đồng 4 tệp, RT-07/FR-SYS-024).

Hợp đồng thư mục — **không đổi** khi phase sau thêm gói mới, vì cả seeder
(`seed.py`, đọc trực tiếp từ đĩa) lẫn `importer.py` (đọc từ `.zip` đã verify chữ
ký) đều đi qua đúng module này:

* `package.json` — khóa `code`, `scheme`, `name`, `name_en`, `description`,
  `legal_reference`, `effective_from` (ISO date), `effective_to` (`null` được),
  `version` (int).
* `accounts.csv` — cột `code,name,name_en,parent_code,balance_nature,
  is_summary,is_foreign_currency,detail_tracking,is_locked`. Cha đứng trước
  con trong tệp; `parent_code` rỗng = gốc; `detail_tracking` là các token của
  `DetailTracking` nối bằng `;`; cột boolean là `0`/`1`.
* `default_accounts.csv` — cột `document_type,purpose,account_code`.
* `closing_pairs.csv` — cột `source_account,target_account,sequence,description`.

**Kiểm hết trước khi trả** (fail-closed): một dòng sai ở giữa tệp 200 dòng thì
toàn bộ gói bị từ chối, không có "nạp một phần". Không dòng nào trong module
này ghi xuống DB — đó là việc của `seed.py`/`importer.py`, module này chỉ đọc
đĩa và trả về dữ liệu **đã kiểm**.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ket.kernel.config.accounts_models import (
    ACCOUNT_CODE_MAX_LENGTH,
    DOCUMENT_TYPE_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
    BalanceNature,
    DetailTracking,
)
from ket.kernel.config.auto_posting_models import OPERATION_CODE_MAX_LENGTH
from ket.kernel.config.statements.formula.evaluator import evaluate_rows
from ket.kernel.config.statements.formula.parser import (
    FormulaNode,
    account_ranges_of,
    parse_formula,
)
from ket.kernel.config.statements.models import (
    FORMULA_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    LAYOUT_CODE_MAX_LENGTH,
    NOTE_REF_MAX_LENGTH,
    ROW_CODE_MAX_LENGTH,
    ROW_NOTE_MAX_LENGTH,
    StatementKind,
)
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import ConfigPackageDataInvalidError, StatementFormulaInvalidError
from ket.kernel.periods.models import AccountingScheme

_PACKAGE_CODE_MAX_LENGTH = 50
_PACKAGE_DESCRIPTION_MAX_LENGTH = 1000
_PACKAGE_REFERENCE_MAX_LENGTH = 255
_CLOSING_DESCRIPTION_MAX_LENGTH = 500
"""Bốn trần này soi gương độ dài cột của `ConfigPackage`/`ClosingAccountPair`
(các cột đó khai độ dài inline, không có hằng số tên). Test
`test_loader_length_limits_match_orm_columns` canh cho hai bên không trôi
khỏi nhau."""

PACKAGE_MANIFEST_FILE = "package.json"
ACCOUNTS_FILE = "accounts.csv"
DEFAULT_ACCOUNTS_FILE = "default_accounts.csv"
CLOSING_PAIRS_FILE = "closing_pairs.csv"
STATEMENTS_FILE = "statements.json"
AUTO_POSTING_RULES_FILE = "auto_posting_rules.csv"

REQUIRED_DATA_FILES: tuple[str, ...] = (
    PACKAGE_MANIFEST_FILE,
    ACCOUNTS_FILE,
    DEFAULT_ACCOUNTS_FILE,
    CLOSING_PAIRS_FILE,
)
"""Danh sách tệp mà một gói **phải** có — dùng chung cho cả thư mục lẫn `.zip`
(`importer.py` chỉ đọc theo danh sách tên cố định này, không đọc theo danh sách
trong gói — xem docstring `importer.py` về chống zip-slip)."""

OPTIONAL_DATA_FILES: tuple[str, ...] = (STATEMENTS_FILE, AUTO_POSTING_RULES_FILE)
"""Tệp một gói **được phép** có thêm — vẫn là tập tên cố định, phẳng, không
thư mục con, để hợp đồng "chỉ những tên tệp cố định, không ghép đường dẫn" của
importer giữ nguyên. Gói không có chúng hợp lệ (gói nhập ngoài có thể chỉ mang
hệ thống TK):

* `statements.json` (lát 5B) — mọi layout BCTC của gói trong MỘT tệp phẳng.
* `auto_posting_rules.csv` (lát 6A) — nghiệp vụ thu/chi và cặp Nợ/Có ngầm
  định (FR-SYS-025)."""

_ACCOUNTS_HEADER = (
    "code",
    "name",
    "name_en",
    "parent_code",
    "balance_nature",
    "is_summary",
    "is_foreign_currency",
    "detail_tracking",
    "is_locked",
)
_DEFAULT_ACCOUNTS_HEADER = ("document_type", "purpose", "account_code")
_CLOSING_PAIRS_HEADER = ("source_account", "target_account", "sequence", "description")
_AUTO_POSTING_RULES_HEADER = (
    "document_type",
    "operation_code",
    "operation_name",
    "debit_purpose",
    "credit_purpose",
    "requires_partner",
    "partner_kind",
    "display_order",
)

_KNOWN_SCHEMES = frozenset(member.value for member in AccountingScheme)
_BALANCE_NATURE_RANGE = range(
    BalanceNature.DEBIT, BalanceNature.NONE + 1
)  # 0..3, hai đầu đều hợp lệ

_DETAIL_TRACKING_SEPARATOR = ";"


@dataclass(frozen=True)
class PackageManifest:
    """Nội dung `package.json`, đã kiểu hóa và kiểm."""

    code: str
    scheme: str
    name: str
    name_en: str | None
    description: str | None
    legal_reference: str | None
    effective_from: date
    effective_to: date | None
    version: int


@dataclass(frozen=True)
class AccountRow:
    """Một dòng `accounts.csv`, đã kiểm — thứ tự trong `LoadedPackage.accounts`
    giữ nguyên thứ tự tệp (cha trước con, đúng bất biến mà `seed.py` cần)."""

    code: str
    name: str
    name_en: str | None
    parent_code: str | None
    balance_nature: int
    is_summary: bool
    is_foreign_currency: bool
    detail_tracking: tuple[str, ...]
    is_locked: bool


@dataclass(frozen=True)
class DefaultAccountRow:
    document_type: str
    purpose: str
    account_code: str


@dataclass(frozen=True)
class ClosingPairRow:
    source_account: str
    target_account: str
    sequence: int
    description: str | None


@dataclass(frozen=True)
class AutoPostingRuleRow:
    """Một dòng `auto_posting_rules.csv`, đã kiểm (FR-SYS-025, lát 6A).

    `debit_purpose`/`credit_purpose` là `purpose` của `default_accounts`;
    `None` = cố ý để ngỏ bên đó cho người dùng chọn — xem docstring
    `kernel/config/auto_posting_models.py`.
    """

    document_type: str
    operation_code: str
    operation_name: str
    debit_purpose: str | None
    credit_purpose: str | None
    requires_partner: bool
    partner_kind: int | None
    display_order: int


@dataclass(frozen=True)
class StatementRowData:
    """Một chỉ tiêu trong `statements.json`, đã kiểm (công thức parse được,
    rowref/dải TK đã đối chiếu). `display_order` suy từ thứ tự trong tệp —
    thứ tự viết chính là thứ tự mẫu thông tư, không khai số riêng để lệch."""

    row_code: str
    label: str
    label_en: str | None
    note_ref: str | None
    formula: str
    indent_level: int
    display_order: int
    is_bold: bool
    hide_when_zero: bool
    note: str | None


@dataclass(frozen=True)
class StatementLayoutData:
    """Một layout BCTC trong `statements.json`, đã kiểm."""

    code: str
    name: str
    name_en: str | None
    statement_kind: str
    rows: tuple[StatementRowData, ...]


@dataclass(frozen=True)
class LoadedPackage:
    """Một gói cấu hình đã đọc và kiểm hết — sẵn sàng để `seed.py`/`importer.py` ghi."""

    manifest: PackageManifest
    accounts: tuple[AccountRow, ...]
    default_accounts: tuple[DefaultAccountRow, ...]
    closing_pairs: tuple[ClosingPairRow, ...]
    statements: tuple[StatementLayoutData, ...] = ()
    auto_posting_rules: tuple[AutoPostingRuleRow, ...] = ()


def _fail(reason: str, **details: str | int | None) -> ConfigPackageDataInvalidError:
    return ConfigPackageDataInvalidError(reason, **details)


def _require_str(value: object, *, field: str, file: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"Trường `{field}` thiếu hoặc rỗng", file=file, field=field)
    return value.strip()


def _bounded(
    value: str | None, *, field: str, file: str, max_length: int, line: int | None = None
) -> str | None:
    """Chặn chuỗi dài hơn cột DB ngay ở lượt kiểm dữ liệu.

    Không kiểm ở đây thì giá trị quá dài sống sót tới lượt flush và đổ
    `DataError` DBAPI thô (HTTP 500) — trái với cam kết fail-closed "kiểm hết
    trước khi trả" của module này và FR-NFR-050 (lỗi nghiệp vụ phải có thông
    điệp nêu nguyên nhân).
    """
    if value is not None and len(value) > max_length:
        raise _fail(
            f"Trường `{field}` dài {len(value)} ký tự, vượt trần {max_length}",
            file=file,
            field=field,
            line=line,
        )
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _parse_date(value: object, *, field: str, file: str) -> date:
    if not isinstance(value, str):
        raise _fail(f"Trường `{field}` phải là chuỗi ngày ISO (YYYY-MM-DD)", file=file, field=field)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise _fail(
            f"Trường `{field}` không đúng định dạng ngày ISO (YYYY-MM-DD)",
            file=file,
            field=field,
            value=value,
        ) from error


def _load_manifest(text: str) -> PackageManifest:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise _fail(
            f"Tệp {PACKAGE_MANIFEST_FILE} không phải JSON hợp lệ: {error}",
            file=PACKAGE_MANIFEST_FILE,
        ) from error
    if not isinstance(raw, Mapping):
        raise _fail(
            f"Tệp {PACKAGE_MANIFEST_FILE} phải là một object JSON", file=PACKAGE_MANIFEST_FILE
        )

    code = _require_str(raw.get("code"), field="code", file=PACKAGE_MANIFEST_FILE)
    scheme = _require_str(raw.get("scheme"), field="scheme", file=PACKAGE_MANIFEST_FILE)
    if scheme not in _KNOWN_SCHEMES:
        raise _fail(
            f"Chế độ kế toán `{scheme}` không thuộc danh sách đã biết",
            file=PACKAGE_MANIFEST_FILE,
            scheme=scheme,
        )
    name = _require_str(raw.get("name"), field="name", file=PACKAGE_MANIFEST_FILE)
    version_raw = raw.get("version")
    if not isinstance(version_raw, int) or isinstance(version_raw, bool) or version_raw < 1:
        raise _fail(
            "Trường `version` phải là số nguyên dương", file=PACKAGE_MANIFEST_FILE, field="version"
        )
    effective_from = _parse_date(
        raw.get("effective_from"), field="effective_from", file=PACKAGE_MANIFEST_FILE
    )
    effective_to_raw = raw.get("effective_to")
    effective_to = (
        None
        if effective_to_raw is None
        else _parse_date(effective_to_raw, field="effective_to", file=PACKAGE_MANIFEST_FILE)
    )
    if effective_to is not None and effective_to <= effective_from:
        raise _fail(
            "`effective_to` phải sau `effective_from`",
            file=PACKAGE_MANIFEST_FILE,
            effective_from=effective_from.isoformat(),
            effective_to=effective_to.isoformat(),
        )

    name_en = _optional_str(raw.get("name_en"))
    description = _optional_str(raw.get("description"))
    legal_reference = _optional_str(raw.get("legal_reference"))
    for field, value, limit in (
        ("code", code, _PACKAGE_CODE_MAX_LENGTH),
        ("name", name, NAME_MAX_LENGTH),
        ("name_en", name_en, NAME_MAX_LENGTH),
        ("description", description, _PACKAGE_DESCRIPTION_MAX_LENGTH),
        ("legal_reference", legal_reference, _PACKAGE_REFERENCE_MAX_LENGTH),
    ):
        _bounded(value, field=field, file=PACKAGE_MANIFEST_FILE, max_length=limit)

    return PackageManifest(
        code=code,
        scheme=scheme,
        name=name,
        name_en=name_en,
        description=description,
        legal_reference=legal_reference,
        effective_from=effective_from,
        effective_to=effective_to,
        version=version_raw,
    )


def _parse_csv_text(text: str, file_name: str, header: tuple[str, ...]) -> list[dict[str, str]]:
    reader = csv.DictReader(text.splitlines())
    found_header = tuple(reader.fieldnames or ())
    if found_header != header:
        raise _fail(
            f"Tiêu đề cột của {file_name} không đúng hợp đồng",
            file=file_name,
            expected=",".join(header),
            found=",".join(found_header),
        )
    return list(reader)


def _parse_bool(value: str, *, field: str, file: str, line: int) -> bool:
    if value not in {"0", "1"}:
        raise _fail(
            f"Trường `{field}` phải là 0 hoặc 1", file=file, field=field, value=value, line=line
        )
    return value == "1"


def _parse_detail_tracking(value: str, *, file: str, line: int, code: str) -> tuple[str, ...]:
    if not value:
        return ()
    tokens = tuple(
        token.strip() for token in value.split(_DETAIL_TRACKING_SEPARATOR) if token.strip()
    )
    unknown = [token for token in tokens if token not in DetailTracking.ALL]
    if unknown:
        raise _fail(
            f"`detail_tracking` của TK {code} chứa token không hợp lệ: {', '.join(unknown)}",
            file=file,
            line=line,
            code=code,
            unknown=", ".join(unknown),
        )
    return tokens


def _load_accounts(text: str) -> tuple[AccountRow, ...]:
    rows = _parse_csv_text(text, ACCOUNTS_FILE, _ACCOUNTS_HEADER)
    seen: dict[str, int] = {}
    result: list[AccountRow] = []
    for line, raw in enumerate(rows, start=2):  # dòng 1 là header
        code = _require_str(raw.get("code"), field="code", file=ACCOUNTS_FILE)
        if code in seen:
            raise _fail(
                f"Số hiệu TK {code} bị khai trùng (đã có ở dòng {seen[code]})",
                file=ACCOUNTS_FILE,
                code=code,
                line=line,
            )
        parent_code = _optional_str(raw.get("parent_code"))
        if parent_code is not None and parent_code not in seen:
            raise _fail(
                f"TK {code} khai `parent_code={parent_code}` nhưng TK cha chưa xuất hiện "
                "trước nó trong tệp (cha phải đứng trước con)",
                file=ACCOUNTS_FILE,
                code=code,
                parent_code=parent_code,
                line=line,
            )
        name = _require_str(raw.get("name"), field="name", file=ACCOUNTS_FILE)
        name_en = _optional_str(raw.get("name_en"))
        _bounded(
            code, field="code", file=ACCOUNTS_FILE, max_length=ACCOUNT_CODE_MAX_LENGTH, line=line
        )
        _bounded(name, field="name", file=ACCOUNTS_FILE, max_length=NAME_MAX_LENGTH, line=line)
        _bounded(
            name_en, field="name_en", file=ACCOUNTS_FILE, max_length=NAME_MAX_LENGTH, line=line
        )
        balance_nature_raw = _require_str(
            raw.get("balance_nature"), field="balance_nature", file=ACCOUNTS_FILE
        )
        try:
            balance_nature = int(balance_nature_raw)
        except ValueError as error:
            raise _fail(
                f"`balance_nature` của TK {code} không phải số nguyên",
                file=ACCOUNTS_FILE,
                code=code,
                line=line,
            ) from error
        if balance_nature not in _BALANCE_NATURE_RANGE:
            raise _fail(
                f"`balance_nature` của TK {code} ngoài khoảng hợp lệ (0-3)",
                file=ACCOUNTS_FILE,
                code=code,
                value=balance_nature,
                line=line,
            )
        result.append(
            AccountRow(
                code=code,
                name=name,
                name_en=name_en,
                parent_code=parent_code,
                balance_nature=balance_nature,
                is_summary=_parse_bool(
                    _require_str(raw.get("is_summary"), field="is_summary", file=ACCOUNTS_FILE),
                    field="is_summary",
                    file=ACCOUNTS_FILE,
                    line=line,
                ),
                is_foreign_currency=_parse_bool(
                    _require_str(
                        raw.get("is_foreign_currency"),
                        field="is_foreign_currency",
                        file=ACCOUNTS_FILE,
                    ),
                    field="is_foreign_currency",
                    file=ACCOUNTS_FILE,
                    line=line,
                ),
                detail_tracking=_parse_detail_tracking(
                    raw.get("detail_tracking") or "", file=ACCOUNTS_FILE, line=line, code=code
                ),
                is_locked=_parse_bool(
                    _require_str(raw.get("is_locked"), field="is_locked", file=ACCOUNTS_FILE),
                    field="is_locked",
                    file=ACCOUNTS_FILE,
                    line=line,
                ),
            )
        )
        seen[code] = line
    return tuple(result)


def _load_default_accounts(text: str, known_codes: frozenset[str]) -> tuple[DefaultAccountRow, ...]:
    rows = _parse_csv_text(text, DEFAULT_ACCOUNTS_FILE, _DEFAULT_ACCOUNTS_HEADER)
    seen: dict[tuple[str, str], int] = {}
    result: list[DefaultAccountRow] = []
    for line, raw in enumerate(rows, start=2):
        document_type = _require_str(
            raw.get("document_type"), field="document_type", file=DEFAULT_ACCOUNTS_FILE
        )
        purpose = _require_str(raw.get("purpose"), field="purpose", file=DEFAULT_ACCOUNTS_FILE)
        account_code = _require_str(
            raw.get("account_code"), field="account_code", file=DEFAULT_ACCOUNTS_FILE
        )
        for field, value, limit in (
            ("document_type", document_type, DOCUMENT_TYPE_MAX_LENGTH),
            ("purpose", purpose, PURPOSE_MAX_LENGTH),
            ("account_code", account_code, ACCOUNT_CODE_MAX_LENGTH),
        ):
            _bounded(value, field=field, file=DEFAULT_ACCOUNTS_FILE, max_length=limit, line=line)
        key = (document_type, purpose)
        if key in seen:
            raise _fail(
                f"Cặp (document_type={document_type}, purpose={purpose}) bị khai trùng "
                f"(đã có ở dòng {seen[key]})",
                file=DEFAULT_ACCOUNTS_FILE,
                document_type=document_type,
                purpose=purpose,
                line=line,
            )
        if account_code not in known_codes:
            raise _fail(
                f"`default_accounts` trỏ tới TK {account_code} không có trong {ACCOUNTS_FILE}",
                file=DEFAULT_ACCOUNTS_FILE,
                account_code=account_code,
                line=line,
            )
        result.append(
            DefaultAccountRow(
                document_type=document_type, purpose=purpose, account_code=account_code
            )
        )
        seen[key] = line
    return tuple(result)


def _load_closing_pairs(text: str, known_codes: frozenset[str]) -> tuple[ClosingPairRow, ...]:
    rows = _parse_csv_text(text, CLOSING_PAIRS_FILE, _CLOSING_PAIRS_HEADER)
    seen: dict[tuple[str, str], int] = {}
    result: list[ClosingPairRow] = []
    for line, raw in enumerate(rows, start=2):
        source_account = _require_str(
            raw.get("source_account"), field="source_account", file=CLOSING_PAIRS_FILE
        )
        target_account = _require_str(
            raw.get("target_account"), field="target_account", file=CLOSING_PAIRS_FILE
        )
        for role, code in (("source_account", source_account), ("target_account", target_account)):
            if code not in known_codes:
                raise _fail(
                    f"`closing_pairs` trỏ tới TK {code} không có trong {ACCOUNTS_FILE}",
                    file=CLOSING_PAIRS_FILE,
                    field=role,
                    account_code=code,
                    line=line,
                )
        key = (source_account, target_account)
        if key in seen:
            raise _fail(
                f"Cặp kết chuyển ({source_account} → {target_account}) bị khai trùng "
                f"(đã có ở dòng {seen[key]})",
                file=CLOSING_PAIRS_FILE,
                source_account=source_account,
                target_account=target_account,
                line=line,
            )
        sequence_raw = _require_str(raw.get("sequence"), field="sequence", file=CLOSING_PAIRS_FILE)
        try:
            sequence = int(sequence_raw)
        except ValueError as error:
            raise _fail(
                "`sequence` của cặp kết chuyển không phải số nguyên",
                file=CLOSING_PAIRS_FILE,
                source_account=source_account,
                target_account=target_account,
                line=line,
            ) from error
        description = _optional_str(raw.get("description"))
        for field, value, limit in (
            ("source_account", source_account, ACCOUNT_CODE_MAX_LENGTH),
            ("target_account", target_account, ACCOUNT_CODE_MAX_LENGTH),
            ("description", description, _CLOSING_DESCRIPTION_MAX_LENGTH),
        ):
            _bounded(value, field=field, file=CLOSING_PAIRS_FILE, max_length=limit, line=line)
        result.append(
            ClosingPairRow(
                source_account=source_account,
                target_account=target_account,
                sequence=sequence,
                description=description,
            )
        )
        seen[key] = line
    return tuple(result)


_MAX_INDENT_LEVEL = 6
"""Trần thụt lề chỉ tiêu — mẫu thông tư sâu nhất (B01 mục 233→235) dùng 4 mức."""

_LAYOUT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
"""Mã layout đi thẳng vào URL path (`GET /statements/{layout_code}/preview`) —
khớp khuôn mã mẫu thông tư (`B01-DN`, `B01a-DNN`), chặn `/`, khoảng trắng, `%`
từ gói `.zip` nhập ngoài (review 5B, M-3)."""


def _statements_fail(
    reason: str, *, layout: str | None = None, row: str | None = None
) -> ConfigPackageDataInvalidError:
    return _fail(reason, file=STATEMENTS_FILE, layout=layout, row=row)


def _statement_required_str(
    raw: Mapping[str, object],
    field: str,
    *,
    max_length: int,
    layout: str | None,
    row: str | None,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _statements_fail(f"Trường `{field}` thiếu hoặc rỗng", layout=layout, row=row)
    stripped = value.strip()
    if len(stripped) > max_length:
        raise _statements_fail(
            f"Trường `{field}` dài {len(stripped)} ký tự, vượt trần {max_length}",
            layout=layout,
            row=row,
        )
    return stripped


def _statement_optional_str(
    raw: Mapping[str, object],
    field: str,
    *,
    max_length: int,
    layout: str | None,
    row: str | None,
) -> str | None:
    if raw.get(field) is None:
        return None
    return _statement_required_str(raw, field, max_length=max_length, layout=layout, row=row)


def _statement_bool(raw: Mapping[str, object], field: str, *, layout: str, row: str) -> bool:
    value = raw.get(field, False)
    if not isinstance(value, bool):
        raise _statements_fail(f"Trường `{field}` phải là true/false", layout=layout, row=row)
    return value


def _load_statement_row(
    raw: object, *, layout_code: str, display_order: int
) -> tuple[StatementRowData, FormulaNode]:
    if not isinstance(raw, Mapping):
        raise _statements_fail(
            f"Chỉ tiêu thứ {display_order} phải là một object JSON", layout=layout_code
        )
    row_code = _statement_required_str(
        raw, "row_code", max_length=ROW_CODE_MAX_LENGTH, layout=layout_code, row=None
    )
    if not row_code.isalnum():
        raise _statements_fail(
            "Mã chỉ tiêu (`row_code`) chỉ được chứa chữ/số", layout=layout_code, row=row_code
        )
    label = _statement_required_str(
        raw, "label", max_length=LABEL_MAX_LENGTH, layout=layout_code, row=row_code
    )
    formula_text = _statement_required_str(
        raw, "formula", max_length=FORMULA_MAX_LENGTH, layout=layout_code, row=row_code
    )
    try:
        node = parse_formula(formula_text)
    except StatementFormulaInvalidError as error:
        raise _statements_fail(
            f"Công thức của chỉ tiêu {row_code} không hợp lệ: {error.message}",
            layout=layout_code,
            row=row_code,
        ) from error

    indent_raw = raw.get("indent_level", 0)
    if (
        not isinstance(indent_raw, int)
        or isinstance(indent_raw, bool)
        or not 0 <= indent_raw <= _MAX_INDENT_LEVEL
    ):
        raise _statements_fail(
            f"`indent_level` phải là số nguyên 0..{_MAX_INDENT_LEVEL}",
            layout=layout_code,
            row=row_code,
        )

    data = StatementRowData(
        row_code=row_code,
        label=label,
        label_en=_statement_optional_str(
            raw, "label_en", max_length=LABEL_MAX_LENGTH, layout=layout_code, row=row_code
        ),
        note_ref=_statement_optional_str(
            raw, "note_ref", max_length=NOTE_REF_MAX_LENGTH, layout=layout_code, row=row_code
        ),
        formula=formula_text,
        indent_level=indent_raw,
        display_order=display_order,
        is_bold=_statement_bool(raw, "is_bold", layout=layout_code, row=row_code),
        hide_when_zero=_statement_bool(raw, "hide_when_zero", layout=layout_code, row=row_code),
        note=_statement_optional_str(
            raw, "note", max_length=ROW_NOTE_MAX_LENGTH, layout=layout_code, row=row_code
        ),
    )
    return data, node


def _check_statement_account_refs(
    rows: dict[str, FormulaNode], known_codes: frozenset[str], *, layout_code: str
) -> None:
    """Mỗi dải TK trong công thức phải khớp ≥1 TK của `accounts.csv` — một dải
    khớp 0 TK gần như chắc chắn là gõ nhầm số hiệu, và giá của nó là một chỉ
    tiêu BCTC lặng lẽ bằng 0 (đúng loại sai "Rất cao" trong bảng rủi ro phase)."""
    for row_code, node in rows.items():
        for account_range in account_ranges_of(node):
            if not any(account_range.matches(code) for code in known_codes):
                raise _statements_fail(
                    f"Dải tài khoản `{account_range.spec()}` trong công thức không khớp "
                    f"tài khoản nào của {ACCOUNTS_FILE}",
                    layout=layout_code,
                    row=row_code,
                )


def _load_statements(text: str, known_codes: frozenset[str]) -> tuple[StatementLayoutData, ...]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise _statements_fail(f"Tệp {STATEMENTS_FILE} không phải JSON hợp lệ: {error}") from error
    if not isinstance(raw, list):
        raise _statements_fail(f"Tệp {STATEMENTS_FILE} phải là một mảng JSON các layout")

    layouts: list[StatementLayoutData] = []
    seen_layouts: set[str] = set()
    for layout_raw in raw:
        if not isinstance(layout_raw, Mapping):
            raise _statements_fail("Mỗi layout phải là một object JSON")
        code = _statement_required_str(
            layout_raw, "code", max_length=LAYOUT_CODE_MAX_LENGTH, layout=None, row=None
        )
        if not _LAYOUT_CODE_PATTERN.fullmatch(code):
            raise _statements_fail(
                "Mã layout chỉ được chứa chữ/số/gạch nối và bắt đầu bằng chữ/số "
                "(mã nằm trên đường dẫn URL của API xem trước)",
                layout=code,
            )
        if code in seen_layouts:
            raise _statements_fail(f"Mã layout {code} bị khai trùng", layout=code)
        seen_layouts.add(code)
        name = _statement_required_str(
            layout_raw, "name", max_length=NAME_MAX_LENGTH, layout=code, row=None
        )
        kind = _statement_required_str(
            layout_raw, "statement_kind", max_length=20, layout=code, row=None
        )
        if kind not in StatementKind.ALL:
            raise _statements_fail(
                f"`statement_kind` `{kind}` không thuộc danh sách đã biết "
                f"({', '.join(sorted(StatementKind.ALL))})",
                layout=code,
            )
        rows_raw = layout_raw.get("rows")
        if not isinstance(rows_raw, list) or not rows_raw:
            raise _statements_fail("Layout phải có mảng `rows` không rỗng", layout=code)

        rows: list[StatementRowData] = []
        nodes: dict[str, FormulaNode] = {}
        for display_order, row_raw in enumerate(rows_raw, start=1):
            data, node = _load_statement_row(row_raw, layout_code=code, display_order=display_order)
            if data.row_code in nodes:
                raise _statements_fail(
                    f"Mã chỉ tiêu {data.row_code} bị khai trùng trong layout",
                    layout=code,
                    row=data.row_code,
                )
            rows.append(data)
            nodes[data.row_code] = node

        # Rowref trỏ ra ngoài layout + chu trình: chạy evaluator trên bộ số
        # RỖNG — rẻ, và dùng đúng bộ luật mà lúc lập báo cáo thật sẽ dùng.
        try:
            evaluate_rows(nodes, {})
        except StatementFormulaInvalidError as error:
            raise _statements_fail(
                f"Bộ công thức của layout không hợp lệ: {error.message}", layout=code
            ) from error
        _check_statement_account_refs(nodes, known_codes, layout_code=code)

        layouts.append(
            StatementLayoutData(
                code=code,
                name=name,
                name_en=_statement_optional_str(
                    layout_raw, "name_en", max_length=NAME_MAX_LENGTH, layout=code, row=None
                ),
                statement_kind=kind,
                rows=tuple(rows),
            )
        )
    return tuple(layouts)


def _load_auto_posting_rules(
    text: str, known_purposes: frozenset[str]
) -> tuple[AutoPostingRuleRow, ...]:
    """Đọc + kiểm `auto_posting_rules.csv` (FR-SYS-025, lát 6A).

    `known_purposes` là tập `purpose` mà `default_accounts.csv` của CHÍNH gói
    này khai. Purpose lạ bị từ chối chứ không lặng lẽ thành "không điền sẵn":
    ô trống là cách duy nhất nói "cố ý để ngỏ", nên gõ nhầm không thể giả dạng
    một quyết định — cùng triết lý với việc `default_accounts` từ chối TK
    không có trong `accounts.csv`.
    """
    rows = _parse_csv_text(text, AUTO_POSTING_RULES_FILE, _AUTO_POSTING_RULES_HEADER)
    seen: dict[tuple[str, str], int] = {}
    result: list[AutoPostingRuleRow] = []
    for line, raw in enumerate(rows, start=2):
        document_type = _require_str(
            raw.get("document_type"), field="document_type", file=AUTO_POSTING_RULES_FILE
        )
        operation_code = _require_str(
            raw.get("operation_code"), field="operation_code", file=AUTO_POSTING_RULES_FILE
        )
        operation_name = _require_str(
            raw.get("operation_name"), field="operation_name", file=AUTO_POSTING_RULES_FILE
        )
        debit_purpose = _optional_str(raw.get("debit_purpose"))
        credit_purpose = _optional_str(raw.get("credit_purpose"))
        for field, value, limit in (
            ("document_type", document_type, DOCUMENT_TYPE_MAX_LENGTH),
            ("operation_code", operation_code, OPERATION_CODE_MAX_LENGTH),
            ("operation_name", operation_name, NAME_MAX_LENGTH),
            ("debit_purpose", debit_purpose, PURPOSE_MAX_LENGTH),
            ("credit_purpose", credit_purpose, PURPOSE_MAX_LENGTH),
        ):
            _bounded(value, field=field, file=AUTO_POSTING_RULES_FILE, max_length=limit, line=line)
        for field, purpose in (
            ("debit_purpose", debit_purpose),
            ("credit_purpose", credit_purpose),
        ):
            if purpose is not None and purpose not in known_purposes:
                raise _fail(
                    f"`{field}` trỏ tới purpose {purpose!r} không có trong "
                    f"{DEFAULT_ACCOUNTS_FILE} của gói",
                    file=AUTO_POSTING_RULES_FILE,
                    field=field,
                    purpose=purpose,
                    line=line,
                )
        requires_partner = _parse_bool(
            (raw.get("requires_partner") or "").strip(),
            field="requires_partner",
            file=AUTO_POSTING_RULES_FILE,
            line=line,
        )
        partner_kind_text = (raw.get("partner_kind") or "").strip()
        partner_kind: int | None = None
        if partner_kind_text:
            try:
                partner_kind = PartnerKind(int(partner_kind_text)).value
            except ValueError as error:
                raise _fail(
                    "`partner_kind` phải là 0 (khách hàng), 1 (nhà cung cấp) "
                    "hoặc 2 (nhân viên), hoặc để trống",
                    file=AUTO_POSTING_RULES_FILE,
                    value=partner_kind_text,
                    line=line,
                ) from error
        display_order_text = (raw.get("display_order") or "").strip()
        try:
            display_order = int(display_order_text)
        except ValueError as error:
            raise _fail(
                "`display_order` phải là số nguyên",
                file=AUTO_POSTING_RULES_FILE,
                value=display_order_text,
                line=line,
            ) from error
        key = (document_type, operation_code)
        if key in seen:
            raise _fail(
                f"Cặp (document_type={document_type}, operation_code={operation_code}) "
                f"bị khai trùng (đã có ở dòng {seen[key]})",
                file=AUTO_POSTING_RULES_FILE,
                document_type=document_type,
                operation_code=operation_code,
                line=line,
            )
        result.append(
            AutoPostingRuleRow(
                document_type=document_type,
                operation_code=operation_code,
                operation_name=operation_name,
                debit_purpose=debit_purpose,
                credit_purpose=credit_purpose,
                requires_partner=requires_partner,
                partner_kind=partner_kind,
                display_order=display_order,
            )
        )
        seen[key] = line
    return tuple(result)


def load_package_from_texts(texts: Mapping[str, str]) -> LoadedPackage:
    """Đọc + kiểm một gói cấu hình từ nội dung **đã có sẵn trong bộ nhớ**.

    Lõi dùng chung của `load_package_directory` (đọc từ đĩa) và
    `importer.py` (đọc từ `.zip` đã verify chữ ký + checksum) — hai nguồn khác
    nhau, cùng một bộ luật kiểm. `texts` phải đủ bốn khóa của
    `REQUIRED_DATA_FILES`; thiếu khóa nào là lỗi lập trình của nơi gọi
    (`KeyError`), không phải lỗi dữ liệu gói — nơi gọi chịu trách nhiệm đọc đủ
    tệp trước khi tới đây.
    """
    manifest = _load_manifest(texts[PACKAGE_MANIFEST_FILE])
    accounts = _load_accounts(texts[ACCOUNTS_FILE])
    known_codes = frozenset(row.code for row in accounts)
    default_accounts = _load_default_accounts(texts[DEFAULT_ACCOUNTS_FILE], known_codes)
    closing_pairs = _load_closing_pairs(texts[CLOSING_PAIRS_FILE], known_codes)
    statements_text = texts.get(STATEMENTS_FILE)
    statements = (
        _load_statements(statements_text, known_codes) if statements_text is not None else ()
    )
    rules_text = texts.get(AUTO_POSTING_RULES_FILE)
    known_purposes = frozenset(row.purpose for row in default_accounts)
    auto_posting_rules = (
        _load_auto_posting_rules(rules_text, known_purposes) if rules_text is not None else ()
    )

    return LoadedPackage(
        manifest=manifest,
        accounts=accounts,
        default_accounts=default_accounts,
        closing_pairs=closing_pairs,
        statements=statements,
        auto_posting_rules=auto_posting_rules,
    )


def load_package_directory(directory: Path) -> LoadedPackage:
    """Đọc + kiểm một thư mục gói cấu hình. Ném `ConfigPackageDataInvalidError`
    ngay tại lỗi đầu tiên tìm thấy — không có gói "nạp một phần".
    """
    if not directory.is_dir():
        raise _fail(f"Thư mục dữ liệu gói cấu hình không tồn tại: {directory}", path=str(directory))
    missing = [name for name in REQUIRED_DATA_FILES if not (directory / name).is_file()]
    if missing:
        raise _fail(
            f"Thiếu tệp bắt buộc: {', '.join(missing)}",
            path=str(directory),
            missing=", ".join(missing),
        )
    texts = {
        name: (directory / name).read_text(encoding="utf-8-sig") for name in REQUIRED_DATA_FILES
    }
    for name in OPTIONAL_DATA_FILES:
        if (directory / name).is_file():
            texts[name] = (directory / name).read_text(encoding="utf-8-sig")
    return load_package_from_texts(texts)


def builtin_data_directory(slug: str) -> Path:
    """Thư mục `data/<slug>` cạnh module này — nguồn của gói dựng sẵn.

    Đường dẫn tương đối theo vị trí gói cài đặt (không `importlib.resources`):
    thư mục `data/` đi thẳng cùng mã nguồn trong wheel/venv, và Path tương đối
    đơn giản hơn API tài nguyên trong khi vẫn hoạt động đúng sau khi cài đặt —
    xem cách `provisioning.find_alembic_config` suy đường dẫn từ vị trí gói.
    """
    return Path(__file__).resolve().parent / "data" / slug


def load_builtin_package(slug: str) -> LoadedPackage:
    """Nạp một gói dựng sẵn theo tên thư mục con của `data/` (`tt99`, `tt133`)."""
    return load_package_directory(builtin_data_directory(slug))


__all__ = [
    "ACCOUNTS_FILE",
    "AUTO_POSTING_RULES_FILE",
    "CLOSING_PAIRS_FILE",
    "DEFAULT_ACCOUNTS_FILE",
    "OPTIONAL_DATA_FILES",
    "PACKAGE_MANIFEST_FILE",
    "REQUIRED_DATA_FILES",
    "STATEMENTS_FILE",
    "AccountRow",
    "AutoPostingRuleRow",
    "ClosingPairRow",
    "DefaultAccountRow",
    "LoadedPackage",
    "PackageManifest",
    "StatementLayoutData",
    "StatementRowData",
    "builtin_data_directory",
    "load_builtin_package",
    "load_package_directory",
    "load_package_from_texts",
]
