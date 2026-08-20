"""Ranh giới công khai của `ket.kernel`.

Mọi thứ module khác được phép chạm phải khai ở đây (Protocol / Pydantic model
/ dataclass có kiểu). Cấm `dict[str, Any]` qua ranh giới module — ADR-015.
Đây cũng là đích neo cho `import-linter` (ADR-004).

Phase 1: cố ý rỗng. Lát 5C mang `Ledger` về đây: hai hệ thống sổ là từ vựng
chung của LD-07 — `posting` ghi theo nó, `reporting` đọc theo nó, mà C5 cấm
reporting import posting; một hằng số chép tay ở mỗi bên là chỗ chúng lệch
nhau. `posting.engine.models` re-export nên mọi import cũ giữ nguyên.
"""

from __future__ import annotations

from enum import IntEnum


class Ledger(IntEnum):
    """Hai hệ thống sổ hoạt động song song (N3, LD-07, FR-NFR-031)."""

    FINANCIAL = 0
    MANAGEMENT = 1
