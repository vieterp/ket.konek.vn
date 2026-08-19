"""Dòng định khoản đã quy đổi — dạng trung gian giữa request và bảng.

Engine quy đổi nguyên tệ → VND **một lần** (`round_money`, FR-NFR-001/002) rồi
cả bộ kiểm lẫn phép INSERT dùng chung kết quả: hai lần làm tròn ở hai chỗ là
hai cơ hội cho chúng lệch nhau. Tách khỏi `validators/__init__.py` để các tệp
validator import được mà không tạo vòng.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ket.posting.engine.requests import PostingLine


@dataclass(frozen=True)
class PreparedLine:
    """Một dòng của một sổ, số quy đổi đã chốt."""

    ledger: int
    line_no: int
    source: PostingLine
    debit: Decimal
    credit: Decimal
