"""Cờ phạm vi công ty cho báo cáo — lát 6G-2.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-31

Chạy **một lần cho mỗi schema dataset** như `0001`..`0022`, bằng `ket_owner`.

Một cột duy nhất: `report_definitions.requires_full_branch_scope` (M-4). Báo
cáo `doi-chieu-ngan-hang` so **hai vế lệch phạm vi** — dòng sao kê là dữ liệu
mức tài khoản (tài khoản ngân hàng dùng chung không mang chi nhánh) còn vế
chứng từ nằm dưới RLS chi nhánh — nên người được cấp một chi nhánh đọc ra phần
lệch phình đúng bằng phần RLS giấu đi. Cờ này chặn cửa đó bằng 403 có lý do.

Bước dữ liệu `_refresh_builtin_data` từng ở đây (chuyển từ 0022 lúc lát 6G-2) và
**đã chuyển tiếp sang `0025`** ở lát 7A. Doctrine từ 6B không đổi, chỉ có cái
đuôi chuỗi thì đổi: `refresh_builtin_reports` đọc dữ liệu builtin đóng gói HIỆN
TẠI và probe SQL từng dataset bằng `LIMIT 0`, nên nó chỉ đúng ở migration CUỐI
chuỗi. 0025 thêm bảng `ar_ap_ledger` mà dataset `ar_ap_aging` đọc; để bước gieo
lại ở đây thì phép probe chạy trước khi bảng tồn tại. Hệ quả cần nhớ: **mỗi lần
chuỗi mọc thêm thứ mà dữ liệu builtin đọc, bước gieo lại phải đi theo xuống
cuối** — bỏ quên là cái bẫy "dataset cũ không nhận backfill" đã cắn ở 6A và
6G-1.

Cột ở đây vẫn phải có TRƯỚC lượt gieo lại dù lượt ấy giờ nằm ở 0025: `refresh`
ghi giá trị cờ vào chính cột này, và thứ tự giữa hai revision do chuỗi bảo đảm.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_full_branch_scope_flag()


def _add_full_branch_scope_flag() -> None:
    """`server_default` false + NOT NULL: mọi báo cáo đang có đều là báo cáo
    một-vế (chạy hẹp thì ra ít dòng hơn, không ra số sai), nên mặc định đúng là
    "không đòi". Bật cờ là việc của từng báo cáo, khai trong metadata."""
    op.add_column(
        "report_definitions",
        sa.Column(
            "requires_full_branch_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("report_definitions", "requires_full_branch_scope")
