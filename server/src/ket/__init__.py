"""Konek — app server phần mềm kế toán Việt Nam (TT200 / TT133).

Modular monolith: `kernel` (nền nghiệp vụ dùng chung) → `posting` (ghi sổ) →
`modules.*` (phân hệ theo SRS). `reporting` chỉ đọc. Luật phụ thuộc ép bằng
`import-linter` (ADR-004); kỷ luật kiểu ép bằng mypy strict (ADR-015).

Kiến trúc: docs/system-architecture.md · Quyết định: docs/adr/README.md
"""

__all__ = ["__version__"]

# Version của app server. Bắt tay client↔server so version schema riêng
# (LD-05 / ADR-003) — không dùng chuỗi này.
__version__ = "0.5.0"
