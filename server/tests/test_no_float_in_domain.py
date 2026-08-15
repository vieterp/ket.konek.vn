"""Cấm `float` trong mã nghiệp vụ — chuẩn code phase 1, ADR-015.

Tiền tệ chỉ được biểu diễn bằng `decimal.Decimal`. Một phép cộng `float` duy
nhất lọt vào đường ghi sổ là đủ làm lệch cân đối và rất khó truy ngược, nên
hàng rào này chạy trong CI ngay từ commit đầu.

Bộ quét đọc AST (không phải regex) nên `float` trong docstring/comment/chuỗi
không bị báo nhầm. Nó bắt:
  * mọi tham chiếu tên `float` — annotation (`x: float`), ép kiểu (`float(x)`),
    union (`Decimal | float`);
  * mọi literal dấu phẩy động (`0.1`).

Ngoại lệ duy nhất được lường trước là mã vẽ biểu đồ (phase 10b) — khi tới đó,
thêm đường dẫn vào `ALLOWED_PREFIXES` **kèm lý do**, không nới bộ quét.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Tiền tố đường dẫn (tương đối với `src/konek`) được miễn, kèm lý do.
ALLOWED_PREFIXES: tuple[str, ...] = ()


@dataclass(frozen=True)
class FloatUsage:
    """Một chỗ dùng `float` bị cấm."""

    file: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — {self.detail}"


def find_float_usages(source: str, filename: str) -> list[FloatUsage]:
    """Quét AST của `source`, trả về danh sách chỗ dùng `float`."""
    usages: list[FloatUsage] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "float":
            usages.append(FloatUsage(filename, node.lineno, "tham chiếu tên `float`"))
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            usages.append(FloatUsage(filename, node.lineno, "thuộc tính `.float`"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            usages.append(
                FloatUsage(filename, node.lineno, f"literal dấu phẩy động `{node.value}`")
            )

    return usages


def _is_allowed(relative_path: str) -> bool:
    return relative_path.startswith(ALLOWED_PREFIXES) if ALLOWED_PREFIXES else False


def test_domain_code_has_no_float(domain_root: Path) -> None:
    """Toàn bộ `src/konek` không được dùng `float`."""
    violations: list[FloatUsage] = []

    for path in sorted(domain_root.rglob("*.py")):
        relative = path.relative_to(domain_root).as_posix()
        if _is_allowed(relative):
            continue
        violations.extend(find_float_usages(path.read_text(encoding="utf-8"), relative))

    assert not violations, "Cấm `float` trong mã nghiệp vụ (ADR-015):\n" + "\n".join(
        str(v) for v in violations
    )


def test_scanner_catches_planted_float() -> None:
    """Bộ quét phải bắt được `float` cố tình cài — nếu không, test trên vô nghĩa."""
    planted = (
        "from decimal import Decimal\n"
        "\n"
        "\n"
        "def tinh_thanh_tien(so_luong: float, don_gia: Decimal) -> Decimal:\n"
        '    """Docstring nhắc chữ float nhưng không phải mã."""\n'
        "    he_so = 1.05\n"
        "    return don_gia * Decimal(str(so_luong * he_so))\n"
    )
    usages = find_float_usages(planted, "planted.py")

    details = [u.detail for u in usages]
    assert any("tham chiếu tên" in d for d in details), "không bắt được annotation float"
    assert any("literal" in d for d in details), "không bắt được literal 1.05"
    assert len(usages) == 2, f"kỳ vọng đúng 2 vi phạm, nhận {details}"


def test_scanner_ignores_float_in_text() -> None:
    """`float` trong docstring/chuỗi không được tính là vi phạm."""
    clean = (
        '"""Module này nói về float nhưng không dùng float."""\n'
        "\n"
        "from decimal import Decimal\n"
        "\n"
        "GHI_CHU = 'không dùng float ở đây'\n"
        "TY_GIA = Decimal('24500.0000')\n"
    )
    assert find_float_usages(clean, "clean.py") == []
