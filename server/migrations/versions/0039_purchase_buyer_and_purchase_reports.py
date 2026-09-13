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

**`_refresh_builtin_data` CHUYỂN TỪ `0035` SANG ĐÂY** — doctrine 5B M-1, và dự án
đã sập bẫy này ba lần (6A, 6G-1, 7A). Revision này gieo mười định nghĩa báo cáo
mới cùng hai dataset của chúng; bản cài đang ở `0038` đã đi qua `0035` từ lâu, nên
không dời bước làm mới lên **head** thì những dataset ấy **không bao giờ** thấy
mười báo cáo mới, và màn hình báo cáo của phân hệ mua hàng sẽ trống trên đúng
những bản cài đang chạy. Kiểm vị trí bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURCHASE_TABLE = "purchase_invoices"


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    op.add_column(_PURCHASE_TABLE, sa.Column("buyer_id", sa.Integer(), nullable=True))
    _refresh_builtin_data()


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này gieo mười báo cáo mua
    hàng và công nợ phải trả, cùng hai dataset của chúng.

    Chỉ chạy online: bước đọc-rồi-ghi không diễn đạt được thành SQL tĩnh của
    `upgrade --sql`. Vị trí của bước này phải luôn ở **head** của chuỗi — xem
    docstring đầu tệp.
    """
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.drop_column(_PURCHASE_TABLE, "buyer_id")
