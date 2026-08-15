"""Cấm ghép chuỗi động vào SQL — luật cứng chốt cùng quyết định B1.

Vì sao luật này là **hàng rào chính**, không phải "làm thêm cho chắc": cô lập
dataset bằng vai trò DB (D3) chặn được tiêm SQL **một câu lệnh**, nhưng không
chặn được tiêm **nhiều câu lệnh** — `"; SET ROLE ds_beta_app; …"` đổi vai trò
sang dataset khác và đọc/ghi được sổ của doanh nghiệp khác. Chi tiết và số đo
trong `docs/adr/adr-017-schema-per-dataset.md`.

Điều khiến nhiều câu lệnh chạy được là psycopg dùng **simple protocol** khi câu
lệnh không có tham số ràng buộc. Có tham số → extended protocol → PostgreSQL
**từ chối** nhiều câu lệnh trong một lần gửi. Nói cách khác: ràng buộc tham số
không chỉ chống tiêm giá trị, nó đóng luôn đường leo sang dataset khác.

Bộ quét đọc AST nên chuỗi trong docstring/comment không bị báo nhầm. Nó bắt lời
gọi `text(...)` / `exec_driver_sql(...)` mà đối số là f-string hoặc phép nối
chuỗi/`%`/`.format()`.

**Ngoại lệ**: tên schema, tên bảng, tên vai trò là **identifier** — SQL không
tham số hóa được chúng. Những chỗ đó ghép chuỗi sau khi đã qua whitelist ký tự
(`validate_schema_name` / `validate_identifier`), và phải khai tường minh dưới
đây **kèm lý do**. Thêm ngoại lệ mới là một quyết định review, không phải một
dòng sửa nhanh.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ALLOWED_FILES: dict[str, str] = {
    "kernel/security/rls.py": "sinh DDL policy + `SET LOCAL search_path`: tên schema/cột là identifier",
    "kernel/security/grants.py": "sinh GRANT/REVOKE: tên bảng, sequence, vai trò là identifier",
    "kernel/security/dataset_roles.py": "sinh DDL vai trò: tên vai trò và schema là identifier",
    "kernel/datasets/bootstrap.py": "cấp quyền bảng điều khiển: tên bảng lấy từ ORM metadata",
    "kernel/datasets/provisioning.py": "CREATE/DROP SCHEMA + đọc `alembic_version`: tên schema là identifier",
}
"""Tệp được phép ghép chuỗi vào SQL, kèm lý do. Mọi tệp ở đây chỉ ghép
**identifier đã qua whitelist**, không bao giờ ghép giá trị do người dùng nhập."""

SQL_CALLS: frozenset[str] = frozenset({"text", "exec_driver_sql", "execute"})


@dataclass(frozen=True)
class Interpolation:
    """Một chỗ ghép chuỗi động vào SQL."""

    file: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — {self.detail}"


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _describe_dynamic(node: ast.expr) -> str | None:
    """Mô tả kiểu ghép chuỗi động, hoặc `None` nếu đối số là chuỗi tĩnh."""
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return "nối chuỗi bằng `+`" if isinstance(node.op, ast.Add) else "định dạng bằng `%`"
    if isinstance(node, ast.Call) and _called_name(node) in {"format", "join"}:
        return "dựng chuỗi bằng `.format()`/`.join()`"
    return None


def find_sql_interpolations(source: str, filename: str) -> list[Interpolation]:
    """Quét AST, trả về các lời gọi SQL nhận chuỗi dựng động."""
    findings: list[Interpolation] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) not in SQL_CALLS:
            continue
        if not node.args:
            continue
        detail = _describe_dynamic(node.args[0])
        if detail is not None:
            findings.append(
                Interpolation(filename, node.lineno, f"{_called_name(node)}(…) nhận {detail}")
            )

    return findings


def test_domain_code_binds_parameters_instead_of_building_sql_strings(domain_root: Path) -> None:
    """Không tệp nào ngoài danh sách ngoại lệ được ghép chuỗi vào SQL."""
    violations: list[Interpolation] = []

    for path in sorted(domain_root.rglob("*.py")):
        relative = path.relative_to(domain_root).as_posix()
        if relative in ALLOWED_FILES:
            continue
        violations.extend(find_sql_interpolations(path.read_text(encoding="utf-8"), relative))

    assert not violations, (
        "Ghép chuỗi vào SQL mở đường tiêm nhiều câu lệnh, và tiêm nhiều câu lệnh "
        "vượt được cô lập dataset (ADR-017 §Consequences). Dùng tham số ràng buộc:\n"
        + "\n".join(str(v) for v in violations)
    )


def test_every_allowed_file_still_exists(domain_root: Path) -> None:
    """Ngoại lệ trỏ tới tệp đã xóa/đổi tên = luật đang nới cho một thứ không còn.

    Không có kiểm này, danh sách ngoại lệ chỉ có thể dài ra.
    """
    missing = [name for name in ALLOWED_FILES if not (domain_root / name).is_file()]
    assert not missing, f"Ngoại lệ trỏ tới tệp không tồn tại: {missing}"


def test_scanner_catches_planted_interpolation() -> None:
    """Bộ quét phải bắt được đúng đường tấn công của B1 — nếu không, test trên vô nghĩa."""
    planted = (
        "def bao_cao(ky: str) -> None:\n"
        "    session.execute(text(f\"SELECT * FROM gl_postings WHERE ky = '{ky}'\"))\n"
        '    session.execute(text("SELECT * FROM gl_postings WHERE ky = " + ky))\n'
    )
    findings = find_sql_interpolations(planted, "planted.py")

    details = [f.detail for f in findings]
    assert any("f-string" in d for d in details), "không bắt được f-string"
    assert any("`+`" in d for d in details), "không bắt được phép nối chuỗi"


def test_scanner_accepts_bound_parameters() -> None:
    """Câu lệnh tĩnh + tham số ràng buộc là cách viết đúng, không được báo nhầm."""
    clean = (
        "def bao_cao(ky: str) -> None:\n"
        '    session.execute(text("SELECT * FROM gl_postings WHERE ky = :ky"), {"ky": ky})\n'
    )
    assert find_sql_interpolations(clean, "clean.py") == []
