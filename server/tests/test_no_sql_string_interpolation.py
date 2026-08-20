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
    "kernel/datasets/provisioning.py": (
        "CREATE/DROP SCHEMA + đọc `alembic_version` của một schema cụ thể: tên schema là "
        "identifier, không tham số hóa được, đã qua `validate_schema_name`"
    ),
    "posting/integrity/runner.py": (
        "Bọc `SELECT count(*)/LIMIT` quanh câu check đọc từ tệp `.sql` ĐÓNG GÓI cùng mã "
        "nguồn (`importlib.resources`, registry đóng) — cùng hạng tin cậy với chuỗi bọc, "
        "không phải dữ liệu ngoài; mọi giá trị động (`branch_id`, `sample_limit`) vẫn đi "
        "qua tham số ràng buộc"
    ),
    "kernel/config/reports/seed.py": (
        "Probe `LIMIT 0` bọc quanh câu SQL từ `compose_scoped_query` trên dữ liệu builtin "
        "ĐÓNG GÓI (`data/builtin_reports.json` + `datasets/*.sql` qua importlib.resources) "
        "— cùng hạng tin cậy với integrity/runner; identifier sắp xếp đã ràng regex ở "
        "`scope.py`, mọi giá trị động đi qua tham số ràng buộc (`_probe_binds`)"
    ),
}
"""Tệp được phép ghép chuỗi vào SQL, kèm lý do.

Danh sách này từng có năm tệp; bốn trong số đó **không** cần miễn trừ — chúng chỉ
*trả về* chuỗi DDL chứ không tự gọi `text()`/`exec_driver_sql()`, nên bộ quét vốn
không đụng tới chúng. Miễn trừ thừa lại có hại thật: `grants.py` và
`dataset_roles.py` là hai tệp sinh SQL nhiều nhất, và một dòng miễn trừ đặt chúng
ra ngoài tầm quét **vĩnh viễn**. Đo bằng cách chạy bộ quét lên từng tệp: bốn tệp
cho 0 phát hiện."""

ALLOWED_MIGRATION_FILES: dict[str, str] = {
    "env.py": (
        '`SET search_path TO "<schema>"` — tên schema là identifier, đã qua '
        "`validate_schema_name`; đây là chỗ duy nhất trong migration được ghép"
    ),
}

SQL_CALLS: frozenset[str] = frozenset({"text", "exec_driver_sql", "execute", "executemany"})
"""Khớp theo **đoạn cuối** của tên gọi, nên `op.execute(...)` và
`connection.exec_driver_sql(...)` đều nằm trong tầm quét."""


@dataclass(frozen=True)
class Interpolation:
    """Một chỗ ghép chuỗi động vào SQL."""

    file: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — {self.detail}"


def _called_name(node: ast.Call) -> str | None:
    """Tên hàm được gọi, giữ cả tiền tố khi nó là một tên đơn (`op.execute`)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return node.func.attr
    return None


def _is_sql_call(node: ast.Call) -> bool:
    name = _called_name(node)
    return name is not None and name.rsplit(".", 1)[-1] in SQL_CALLS


def _describe_dynamic(node: ast.expr) -> str | None:
    """Mô tả kiểu ghép chuỗi động, hoặc `None` nếu đối số là chuỗi tĩnh."""
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return "nối chuỗi bằng `+`" if isinstance(node.op, ast.Add) else "định dạng bằng `%`"
    if isinstance(node, ast.Call) and (_called_name(node) or "").rsplit(".", 1)[-1] in {
        "format",
        "join",
    }:
        return "dựng chuỗi bằng `.format()`/`.join()`"
    return None


def _dynamic_locals(tree: ast.Module) -> dict[str, str]:
    """Biến gán **một lần** từ một chuỗi dựng động, tra theo tên.

    `sql = f"SELECT … {v}"` rồi `text(sql)` là cách viết tự nhiên nhất cho một
    câu truy vấn báo cáo dài — tức đúng thứ phase 5 sẽ viết — và bộ quét chỉ nhìn
    đối số tại chỗ sẽ bỏ lọt hoàn toàn. Chỉ nhận biến gán **đúng một lần** trong
    tệp: gán nhiều lần thì không biết giá trị nào tới được lời gọi, và đoán ở đây
    sẽ sinh báo nhầm.
    """
    assigned: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigned.setdefault(node.target.id, []).append(node.value)

    dynamic: dict[str, str] = {}
    for name, values in assigned.items():
        if len(values) != 1:
            continue
        detail = _describe_dynamic(values[0])
        if detail is not None:
            dynamic[name] = f"biến `{name}` gán từ {detail}"
    return dynamic


def _unwrap(node: ast.expr) -> ast.expr:
    """Bóc các lớp bọc chỉ định dạng lại chuỗi, không đổi bản chất động của nó."""
    while isinstance(node, ast.Call) and (_called_name(node) or "").rsplit(".", 1)[-1] in {
        "dedent",
        "strip",
        "format_map",
    }:
        if not node.args:
            break
        node = node.args[0]
    return node


def find_sql_interpolations(source: str, filename: str) -> list[Interpolation]:
    """Quét AST, trả về các lời gọi SQL nhận chuỗi dựng động."""
    findings: list[Interpolation] = []
    tree = ast.parse(source, filename=filename)
    dynamic_locals = _dynamic_locals(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_sql_call(node):
            continue
        if not node.args:
            continue
        argument = _unwrap(node.args[0])
        detail = _describe_dynamic(argument)
        if detail is None and isinstance(argument, ast.Name):
            detail = dynamic_locals.get(argument.id)
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


def test_migrations_also_bind_parameters(domain_root: Path) -> None:
    """Migration là nơi ghép chuỗi nhiều nhất — không được nằm ngoài tầm quét.

    `op.execute(...)` cũng là một đường chạy SQL; nó không đi qua `text()` nên
    bộ quét phải biết tên đó.
    """
    migrations_root = domain_root.parent.parent / "migrations"
    violations: list[Interpolation] = []

    for path in sorted(migrations_root.rglob("*.py")):
        relative = path.relative_to(migrations_root).as_posix()
        if relative in ALLOWED_MIGRATION_FILES:
            continue
        violations.extend(find_sql_interpolations(path.read_text(encoding="utf-8"), relative))

    assert not violations, "Ghép chuỗi vào SQL trong migration:\n" + "\n".join(
        str(v) for v in violations
    )


def test_scanner_catches_the_indirect_forms(domain_root: Path) -> None:
    """Ba đường vòng mà bản đầu của bộ quét bỏ lọt."""
    planted = (
        "from textwrap import dedent\n"
        "def bao_cao(ky: str) -> None:\n"
        "    sql = f\"SELECT * FROM gl_postings WHERE ky = '{ky}'\"\n"
        "    session.execute(text(sql))\n"
        '    conn.exec_driver_sql(dedent(f"SELECT {ky}"))\n'
        '    op.execute(f"GRANT SELECT ON {ky} TO r")\n'
    )
    details = [f.detail for f in find_sql_interpolations(planted, "planted.py")]

    assert any("biến `sql`" in d for d in details), "bỏ lọt biến trung gian"
    assert any("exec_driver_sql" in d for d in details), "bỏ lọt dedent(f-string)"
    assert any("op.execute" in d for d in details), "bỏ lọt op.execute"


def test_no_exemption_is_unnecessary(domain_root: Path) -> None:
    """Mỗi tệp trong danh sách miễn trừ phải **thật sự** bị bộ quét bắt.

    Miễn trừ thừa đặt một tệp ra ngoài tầm quét vĩnh viễn mà không ai để ý — và
    bốn trong năm miễn trừ ban đầu đúng là thừa.
    """
    unnecessary = [
        name
        for name in ALLOWED_FILES
        if not find_sql_interpolations((domain_root / name).read_text(encoding="utf-8"), name)
    ]
    assert not unnecessary, f"Miễn trừ không cần thiết, bỏ đi: {unnecessary}"


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
