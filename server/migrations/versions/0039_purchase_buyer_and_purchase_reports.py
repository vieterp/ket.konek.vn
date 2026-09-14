"""Người mua trên chứng từ mua + mười báo cáo mua hàng / công nợ phải trả — lát 7G-1.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-12

Chạy **một lần cho mỗi schema dataset** như `0001`..`0038`, bằng `ket_owner`.

**Một cột, và nó là điều kiện tồn tại của một chiều báo cáo.** FR-PUR-040 đòi
mọi báo cáo mua hàng lọc và gộp được theo **nhân viên**, và SRS 05 §5 nêu chiều
ấy đích danh trong hai báo cáo (#1 tổng hợp mua hàng, #4 tổng hợp công nợ phải
trả). `sales_invoices` đã có `salesperson_id` từ `0028`; `purchase_invoices` thì
không có cột nào cho người mua, nên chiều gộp ấy không có nguồn — thêm một layout
gộp theo nhân viên mà không có cột thì được một báo cáo chỉ có đúng một nhóm
rỗng. Cột thêm **bây giờ** còn rẻ: thêm sau khi đã có dữ liệu mua hàng thật thì
mọi chứng từ cũ mang giá trị trống và không ai đi điền lại được.

Không khóa ngoại tới `employees`, đối xứng `sales_invoices.salesperson_id`: vòng
đời danh mục trong hệ này canh bằng **bộ đếm tham chiếu** (`record_use`, xem
`PurchaseInvoiceService._usage_of`), không bằng `RESTRICT` của PostgreSQL. Một
khóa ngoại thật ở đây sẽ là luật thứ hai cho cùng một câu hỏi "xóa được nhân
viên này chưa", và hai luật trên một câu hỏi là chỗ chúng trả lời khác nhau.

`GRANT` không phải cấp lại: `GRANT` của PostgreSQL là theo bảng chứ không theo
cột, nên cột mới nằm trong quyền `purchase_invoices` đã cấp từ `0026` (tiền lệ
`0022`, `0035`). RLS cũng không đổi — phạm vi chi nhánh vẫn là của header
`vouchers`.

Bước dữ liệu `_refresh_builtin_data` **từng ở đây** (chuyển `0035` → `0039` ở lát
7G-1), đã **chuyển tiếp sang `0040`** ở lát 7G-2a: bước ấy đọc manifest builtin
HIỆN TẠI, nên nó chỉ gieo đủ khi đứng ở **head** của chuỗi — bản cài đã đi qua
revision này trước khi manifest có chín báo cáo bán hàng thì sẽ không bao giờ thấy
chúng. Doctrine 5B M-1, và dự án đã sập bẫy này bốn lần (6A, 6G-1, 7A, 7G-1).
Luật siết lại sau 7G-1: **đúng một lượt gọi, ở head**. Kiểm vị trí bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURCHASE_TABLE = "purchase_invoices"


def upgrade() -> None:
    op.add_column(_PURCHASE_TABLE, sa.Column("buyer_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_PURCHASE_TABLE, "buyer_id")
