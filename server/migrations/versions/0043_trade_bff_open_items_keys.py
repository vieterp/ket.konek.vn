"""Bốn khóa máy trên dataset công nợ cho hai BFF mua/bán — lát 7G-4.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-17

Chạy **một lần cho mỗi schema dataset** như `0001`..`0042`, bằng `ket_owner`.

Revision này **không đổi schema**. Lát 7G-4 dựng hai BFF chỉ-đọc (tab "việc
còn thiếu" mua/bán và thẻ công nợ đối tác) đọc chính dataset `ar_ap_open_items`
của báo cáo tuổi nợ qua `api/open_items.py` — thay vì chép khối UNION hai nguồn
lần thứ ba. Để mở được đúng chứng từ và lọc đúng người, dataset ấy chiếu thêm
bốn cột máy (`document_id`, `partner_kind`, `partner_id`, `target_kind`); không
layout nào in chúng, nên mọi báo cáo đang chạy giữ nguyên tờ giấy.

**`_refresh_builtin_data` và lượt dọn TỪNG Ở ĐÂY** (lát 7G-4, chuyển từ `0042`),
rồi **cả hai cùng dời sang `0044`** ở lát 7G-5 — doctrine 5B M-1 đòi bước làm mới
đậu ở **head** của chuỗi. Lượt dọn đi theo vì không tách rời được (xem `0041`).

Luật giữ nguyên: **đúng một lượt gọi, ở head**. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Không còn việc gì ở revision này — **có chủ đích**, cùng lý do `0041`/`0042`:
    bước làm mới metadata builtin và lượt dọn `ar_ap_aging` đã dời lên head
    (`0044`) ở lát 7G-5. Revision giữ chỗ vì một mã revision biến mất khỏi chuỗi
    là một bản cài không nâng cấp tiếp được.
    """


def downgrade() -> None:
    """Không có gì để hoàn: revision này không đổi schema, và metadata builtin
    được gieo lại từ manifest của bản phát hành tương ứng."""
