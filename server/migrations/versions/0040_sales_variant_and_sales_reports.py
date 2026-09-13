"""Quy cách trên dòng hàng bán + chín báo cáo bán hàng — lát 7G-2a.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-13

Chạy **một lần cho mỗi schema dataset** như `0001`..`0039`, bằng `ket_owner`.

Cùng revision thêm **index một phần trên `gl_postings.source_line_id`** — đường
nối mà cả `purchase_register` (7G-1) lẫn `sales_register` (lát này) tương quan
theo, và tới nay chưa có index nào. Nó thuộc bảng dùng chung chứ không thuộc
phân hệ bán, nhưng lát này là lát đưa phân hệ có lượng chứng từ lớn nhất lên
đường ấy, và thêm index khi bảng còn nhỏ rẻ hơn nhiều lần.

**Một cột, và nó là điều kiện tồn tại của một chiều báo cáo.** SRS 06 §5.1 #3
đòi "sổ chi tiết bán hàng theo mã quy cách". Danh mục quy cách đã có từ phase 3
(`item_variants`, duy nhất theo cặp mã hàng + mã quy cách), nhưng `sales_invoice_lines`
không mang cột nào trỏ tới nó, nên chiều gộp ấy **không có nguồn** — thêm một
layout gộp theo quy cách mà không có cột thì được một báo cáo chỉ có đúng một
nhóm rỗng. Cột thêm **bây giờ** còn rẻ: thêm sau khi đã có dữ liệu bán hàng thật
thì mọi chứng từ cũ mang giá trị trống và không ai đi điền lại được.

Không khóa ngoại tới `item_variants`, cùng lối `item_id`/`unit_id`/`warehouse_id`
của chính bảng này: vòng đời danh mục trong hệ này canh bằng **bộ đếm tham chiếu**
(`record_use`), không bằng `RESTRICT` của PostgreSQL, và hai luật trên cùng câu
hỏi "xóa được dòng danh mục này chưa" là chỗ chúng trả lời khác nhau. Ràng buộc
thật sự cần canh ở đây là một **cặp** — quy cách phải thuộc đúng mã hàng của dòng
— mà một khóa ngoại một cột không diễn đạt được; nó nằm ở
`SalesInvoiceService._verify_variants_belong_to_items`.

`GRANT` không phải cấp lại: `GRANT` của PostgreSQL là theo bảng chứ không theo
cột, nên cột mới nằm trong quyền `sales_invoice_lines` đã cấp từ `0028` (tiền lệ
`0022`, `0035`, `0039`). RLS cũng không đổi — phạm vi chi nhánh vẫn là của header
`vouchers`.

**`_refresh_builtin_data` CHUYỂN TỪ `0039` SANG ĐÂY** — doctrine 5B M-1, và dự án
đã sập bẫy này bốn lần (6A, 6G-1, 7A, rồi 7G-1 nơi hai lượt gọi cùng tồn tại và
cả chuỗi migration đứt vì lượt đứng giữa chạy thử SQL của hôm nay trên schema của
hôm đó). Revision này gieo chín định nghĩa báo cáo mới cùng dataset
`sales_register` của chúng; bản cài đang ở `0039` đã đi qua bước làm mới của
`0039` **trước khi** manifest có chín báo cáo ấy, nên không dời bước làm mới lên
**head** thì màn hình báo cáo của phân hệ bán hàng sẽ trống trên đúng những bản
cài đang chạy.

Luật siết lại sau 7G-1: **đúng một lượt gọi, ở head**. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SALES_LINE_TABLE = "sales_invoice_lines"
_POSTINGS_TABLE = "gl_postings"
_SOURCE_LINE_INDEX = "ix_gl_postings_source_line_id"


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    op.add_column(_SALES_LINE_TABLE, sa.Column("variant_id", sa.Integer(), nullable=True))
    # Index một phần cho đường nối của MỌI dataset sổ chi tiết mua/bán.
    # `gl_postings` chưa có index nào trên `source_line_id`, mà cả hai LATERAL của
    # `purchase_register` (7G-1) và `sales_register` (lát này) tương quan đúng theo
    # cột ấy: planner phải rơi về `(ledger, posting_date)` rồi lọc, nên chi phí
    # tăng theo (số dòng hàng trong kỳ × số phát sinh của tài khoản trong kỳ) — và
    # 511/156 là những tài khoản dày nhất trong sổ. Thêm bây giờ còn rẻ.
    #
    # `WHERE source_line_id IS NOT NULL` vì phần lớn dòng phát sinh không đến từ
    # một dòng hàng nào (bút toán tay, chênh lệch tỷ giá của lượt đối trừ — xem
    # `posting.settlements.fx_adjustment_lines`), và index một phần không phải
    # mang theo chúng.
    op.create_index(
        _SOURCE_LINE_INDEX,
        _POSTINGS_TABLE,
        ["source_line_id"],
        unique=False,
        postgresql_where=sa.text("source_line_id IS NOT NULL"),
    )
    _refresh_builtin_data()


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này gieo chín báo cáo bán
    hàng cùng dataset `sales_register` của chúng.

    Chỉ chạy online: bước đọc-rồi-ghi không diễn đạt được thành SQL tĩnh của
    `upgrade --sql`. Vị trí của bước này phải luôn ở **head** của chuỗi, và chỉ
    có **một** lượt gọi trong cả chuỗi — xem docstring đầu tệp.
    """
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.drop_index(_SOURCE_LINE_INDEX, table_name=_POSTINGS_TABLE)
    op.drop_column(_SALES_LINE_TABLE, "variant_id")
