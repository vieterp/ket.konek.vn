"""Cấm literal số hiệu tài khoản trong `modules/**` và `posting/**` (phase-05 bước 5).

Rủi ro đắt nhất của phần mềm loại này (SRS 19 §9 #1): hard-code hệ thống tài
khoản. Một dòng `if account.code == "641":` sống sót qua review vẫn đúng cho
tới ngày khách hàng kích hoạt gói TT133 — nơi TK 641 không tồn tại (TT133 gộp
"chi phí bán hàng" vào 642) — và dòng code đó âm thầm không bao giờ chạy nữa.
Chỗ thay thế đúng là `accounts_provider.default_account(purpose=…)` (phase-05
§Quy tắc đọc, ngoại lệ có kiểm soát).

Quét AST tìm chuỗi khớp `^[1-9][0-9]{2,4}$` (dạng số hiệu TK 3-5 chữ số) xuất
hiện trong **so sánh** (`==`, `!=`, `in`) hoặc **lời gọi hàm** — hai chỗ literal
số hiệu TK thực sự điều khiển hành vi. Docstring/comment không bị báo nhầm: AST
chỉ thấy giá trị chuỗi *đúng bằng* một mã 3-5 chữ số, không thấy chuỗi đó nằm
lẫn trong một đoạn văn dài hơn.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

ACCOUNT_LITERAL_PATTERN = re.compile(r"^[1-9][0-9]{2,4}$")

SCANNED_SUBDIRS: tuple[str, ...] = ("modules", "posting", "reporting")
"""Ba tầng nghiệp vụ được quét — kernel không hạch toán trực tiếp, và
`accounts_provider`/gói cấu hình chính là nơi **được phép** cầm số hiệu TK.
`reporting` vào danh sách từ lát 5B: công thức chỉ tiêu là DỮ LIỆU của gói
(`statements.json`), builder chỉ được đọc chúng — một số hiệu TK viết cứng
trong builder là đường vòng qua chính cơ chế layout mà nó phục vụ."""

ALLOWLIST: dict[tuple[str, int], str] = {}
"""`(đường dẫn tương đối tới domain_root, dòng)` → lý do được phép.

Rỗng có chủ đích: chưa có chỗ nào trong `modules/**`/`posting/**` cần một
literal số hiệu TK — mọi logic đã đi qua `default_accounts`/`closing_account_pairs`
(FR-SYS-023/024). Thêm một dòng vào đây là một quyết định review, kèm lý do vì
sao chỗ đó **không** dùng resolver được."""


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    code: str
    context: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — literal `{self.code}` trong {self.context}"


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return "<call>"


def _account_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if ACCOUNT_LITERAL_PATTERN.fullmatch(node.value):
            return node.value
    return None


def _flattened_operands(node: ast.expr) -> list[ast.expr]:
    """Một toán hạng của so sánh — bung `("111", "112")`/`["111", "112"]` khi
    toán hạng là tập hằng dùng cho `in`/`not in` (`code in ("111", "112")`).
    """
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return list(node.elts)
    return [node]


def find_account_literals(source: str, filename: str) -> list[Finding]:
    """Quét AST một tệp, trả về literal số hiệu TK dùng trong so sánh/lời gọi."""
    findings: list[Finding] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            has_membership_or_equality = any(
                isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn) for op in node.ops
            )
            if not has_membership_or_equality:
                continue
            operands = [node.left, *node.comparators]
            for operand in [flat for raw in operands for flat in _flattened_operands(raw)]:
                code = _account_literal(operand)
                if code is not None:
                    findings.append(Finding(filename, node.lineno, code, "một phép so sánh"))
        elif isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            for argument in arguments:
                code = _account_literal(argument)
                if code is not None:
                    findings.append(
                        Finding(filename, node.lineno, code, f"lời gọi `{_called_name(node)}(…)`")
                    )

    return findings


def _iter_scanned_files(domain_root: Path) -> list[Path]:
    files: list[Path] = []
    for subdir in SCANNED_SUBDIRS:
        directory = domain_root / subdir
        if not directory.is_dir():
            # `modules/` chưa tồn tại ở lát này (phase 6 trở đi mới đổ vào) —
            # bỏ qua thay vì lỗi, nhưng vẫn quét lại mỗi lần chạy nên module
            # đầu tiên đổ vào sẽ bị chặn ngay từ commit đầu, không phải chờ ai
            # nhớ ra thêm test.
            continue
        files.extend(
            path for path in sorted(directory.rglob("*.py")) if not path.name.startswith("test_")
        )
    return files


def test_no_hardcoded_account_literal_in_business_layers(domain_root: Path) -> None:
    """`modules/**` và `posting/**` không được so sánh/gọi hàm bằng literal số hiệu TK."""
    violations: list[Finding] = []

    for path in _iter_scanned_files(domain_root):
        relative = path.relative_to(domain_root).as_posix()
        source = path.read_text(encoding="utf-8")
        for finding in find_account_literals(source, relative):
            if (relative, finding.line) in ALLOWLIST:
                continue
            violations.append(finding)

    assert not violations, (
        "Literal số hiệu TK điều khiển hành vi ngoài tầm gói cấu hình (SRS 19 §9 #1). "
        "Dùng `accounts_provider.default_account(session, package_id=…, document_type=…, "
        "purpose=…)` thay vì so sánh/gọi hàm bằng một số hiệu cứng:\n"
        + "\n".join(str(v) for v in violations)
    )


def test_allowlist_entries_still_exist_and_are_necessary(domain_root: Path) -> None:
    """Mỗi dòng miễn trừ phải trỏ tới một tệp có thật và **thật sự** bị bộ quét bắt.

    Cùng khuôn với `test_no_sql_string_interpolation.py`: miễn trừ thừa đặt một
    dòng ra ngoài tầm quét vĩnh viễn mà không ai để ý.
    """
    unnecessary: list[str] = []
    for (relative, line), _reason in ALLOWLIST.items():
        path = domain_root / relative
        if not path.is_file():
            unnecessary.append(f"{relative}:{line} (tệp không tồn tại)")
            continue
        findings = find_account_literals(path.read_text(encoding="utf-8"), relative)
        if not any(finding.line == line for finding in findings):
            unnecessary.append(f"{relative}:{line} (bộ quét không bắt được ở dòng này)")

    assert not unnecessary, f"Miễn trừ không cần thiết, bỏ đi: {unnecessary}"


def test_scanner_catches_equality_comparison() -> None:
    planted = 'def resolve(code: str) -> bool:\n    return code == "641"\n'
    findings = find_account_literals(planted, "planted.py")
    assert any(f.code == "641" for f in findings), "không bắt được so sánh bằng"


def test_scanner_catches_membership_check() -> None:
    planted = 'def resolve(code: str) -> bool:\n    return code in ("111", "112")\n'
    findings = find_account_literals(planted, "planted.py")
    assert {f.code for f in findings} == {"111", "112"}, "không bắt được kiểm tra `in`"


def test_scanner_catches_call_argument() -> None:
    planted = 'def resolve(session: object) -> None:\n    lookup_account(session, "33311")\n'
    findings = find_account_literals(planted, "planted.py")
    assert any(f.code == "33311" for f in findings), "không bắt được đối số lời gọi"


def test_scanner_ignores_docstrings_and_prose() -> None:
    """Một đoạn văn dài chứa `"641"` giữa câu không phải một literal độc lập."""
    planted = (
        "def resolve() -> None:\n"
        '    """TK khớp một tiền tố trong applies_to_accounts ('
        "'641' phủ 6411).\n"
        '    """\n'
    )
    assert find_account_literals(planted, "planted.py") == []


def test_scanner_ignores_numeric_strings_outside_the_length_window() -> None:
    """Chuỗi số ngoài 3-5 chữ số (2 chữ số, 6+ chữ số) không khớp mẫu — không phải mọi
    chuỗi số nào cũng bị coi là số hiệu TK, chỉ đúng dải độ dài mã TK thật dùng.
    """
    planted = 'def resolve(code: str) -> bool:\n    return code == "26" or code == "1234567"\n'
    assert find_account_literals(planted, "planted.py") == []
