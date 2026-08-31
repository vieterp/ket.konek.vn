"""Gieo lại mẫu in builtin — lát 6E-2.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-22

Chạy **một lần cho mỗi schema dataset** như `0001`..`0020`, bằng `ket_owner`.

**Migration không có cột nào** — cố ý. Lát 6E-2 chỉ thêm DỮ LIỆU: năm mẫu in
builtin (01-TT, 02-TT, ủy nhiệm chi, giấy báo có, 08a-TT). Mà mẫu builtin chỉ
được gieo ở hai chỗ: lúc cấp dataset mới, và ở bước `_refresh_builtin_data`
của migration mới nhất. Dữ liệu kế toán đã chạy tới `0020` sẽ **không bao giờ**
thấy năm mẫu đó nếu không có migration này — đúng cái bẫy "dataset cũ không
nhận backfill" đã cắn ở lát 6A (doctrine 5B M-1).

Bước dữ liệu ĐÃ RỜI SANG 0022 (lát 6G-1), đúng doctrine từ 6B mà chính tệp này
phát biểu: `refresh_builtin_reports` đọc dữ liệu builtin đóng gói HIỆN TẠI và
probe SQL từng dataset, nên nó chỉ đúng ở migration CUỐI chuỗi. 0022 thêm cột
`gl_postings.bank_account_id` mà ba dataset builtin đọc — để bước dữ liệu ở lại
đây thì lượt nâng cấp nổ ngay tại 0021, trước khi cột kịp tồn tại.

Migration này vì thế còn lại **rỗng**, và ở lại chuỗi chứ không bị xóa: dataset
nào đã chạy qua nó mang `alembic_version = '0021'`, và bỏ một revision khỏi
chuỗi là làm mọi bản cài ấy không nâng cấp được nữa.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rỗng — bước dữ liệu đã rời sang 0022 (xem docstring đầu tệp)."""


def downgrade() -> None:
    """Rỗng, đối xứng với `upgrade`."""
