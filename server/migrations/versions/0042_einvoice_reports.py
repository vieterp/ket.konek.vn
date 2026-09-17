"""Bốn báo cáo hóa đơn điện tử + dải số hóa đơn — lát 7G-3.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-17

Chạy **một lần cho mỗi schema dataset** như `0001`..`0041`, bằng `ket_owner`.
Revision này **không đổi schema** — lát 7G-3 đọc nguyên những bảng 7D/7E/7F đã
dựng (`einvoices`, `invoice_registrations`, `invoice_forms`) và không thêm cột
nào.

**`_refresh_builtin_data` và lượt dọn TỪNG Ở ĐÂY** (lát 7G-3, chuyển từ `0041`),
rồi **cả hai cùng dời sang `0043`** ở lát 7G-4 — doctrine 5B M-1 đòi bước làm mới
đậu ở **head** của chuỗi, vì nó chạy thử SQL của manifest HÔM NAY trên schema của
revision nó đứng. Lượt dọn đi theo vì không tách rời được (xem docstring `0041`).

Luật giữ nguyên: **đúng một lượt gọi, ở head**. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Không còn việc gì ở revision này — **có chủ đích**, cùng lý do `0041`:
    bước làm mới metadata builtin và lượt dọn `ar_ap_aging` đã dời lên head
    (`0043`) ở lát 7G-4. Revision giữ chỗ vì một mã revision biến mất khỏi chuỗi
    là một bản cài không nâng cấp tiếp được.
    """


def downgrade() -> None:
    """Không có gì để hoàn: revision này không đổi schema, và metadata builtin
    được gieo lại từ manifest của bản phát hành tương ứng."""
