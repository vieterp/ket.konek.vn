"""Bật lại ràng buộc `CHECK` liệt kê giá trị cho `items.nature` (H90).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18

Chạy **một lần cho mỗi schema dataset** như `0001`..`0006`, bằng `ket_owner`.

Đảo một quyết định của lát 3B-3, có chủ đích và có lý do mới. 3B-3 khai
`Enum(..., create_constraint=False)` với lập luận: bất biến thật đã nằm ở
`nature_set_unless_group`, và một `CHECK` liệt kê nữa là chỗ thứ hai phải sửa mỗi
lần thêm một tính chất.

Lập luận ấy đúng **khi mọi đường ghi đều đi qua Pydantic**, thứ vốn đã ép enum.
Lát 3C-1 thêm một đường ghi không qua Pydantic: nhập liệu từ Excel ghi bằng
`INSERT ... SELECT` trên bảng đệm, nên một ô gõ sai đặt được `'khong_ton_tai'` vào
cột `varchar(20)` này.

Hậu quả không nằm ở dòng đó — và đó là lý do nó đáng một migration riêng.
SQLAlchemy đọc cột thành `ItemNature`, nên sau đấy **mọi** lượt đọc `Item` qua ORM
ném `LookupError`: `GET /api/v1/master/items` hỏng cho cả dữ liệu kế toán, và bản
ghi sai không sửa hay xóa được từ giao diện, vì mọi đường sửa cũng phải đọc nó lên
trước. Một ô gõ sai làm hỏng một màn hình cho tới khi có người sửa DB bằng tay.

Tầng nhập liệu cũng kiểm (`staging._allowed_value_errors`, trả câu tiếng Việt nêu
đúng ô). Hai lớp, đúng khuôn H76 của chính 3B-3: **`CHECK` là chỗ bảo đảm, tầng
API là chỗ nói**. Phase 5 thêm một đường ghi không qua Pydantic nữa (gói cấu hình
chạy SQL), nên lớp ở DB là lớp duy nhất bao được cả ba.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS = "items"
_CONSTRAINT = "nature_known"
"""Tên **chưa** mang tiền tố: quy ước đặt tên của dự án tự thêm `ck_<bảng>_`."""

_NATURES = "'goods', 'finished_goods', 'service', 'description_only'"
"""Bốn giá trị của `ItemNature`, viết nguyên văn.

Migration là **ảnh chụp lịch sử**: đọc enum của mã nguồn sẽ khiến bản này âm thầm
đổi nghĩa mỗi lần phase sau thêm một tính chất. Thêm tính chất = thêm một
migration đổi ràng buộc, và đó chính là chỗ thứ hai mà 3B-3 muốn tránh — cái giá
đã được cân nhắc lại và chấp nhận."""


def upgrade() -> None:
    # `NULL` vẫn hợp lệ: nút nhóm không có tính chất (`nature_set_unless_group`
    # canh chiều ngược lại — nút không phải nhóm thì bắt buộc có).
    op.create_check_constraint(_CONSTRAINT, _ITEMS, f"nature IS NULL OR nature IN ({_NATURES})")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _ITEMS, type_="check")
