"""Bản chất bút toán: `entry_kind` trên `vouchers` + `gl_postings` (LD-17).

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-20

Chạy **một lần cho mỗi schema dataset** như `0001`..`0011`, bằng `ket_owner`.

Vì sao cột này tồn tại (review 5B, H1): bút toán kết chuyển cuối kỳ đổ vào
**chính** những phát sinh mà công thức B02 đọc (Nợ 511 / Có 911). Không phân
biệt được hai loại thì trên một năm đã kết chuyển 9/10 chỉ tiêu B02 sai, và
không trạng thái dữ liệu nào cứu được — năm muốn cân bảng cân đối thì bắt buộc
đã kết chuyển. Xem `EntryKind` (`posting/documents/models.py`) về ngữ nghĩa và
ai đọc gì.

Hai cột, một nguồn sự thật: `vouchers.entry_kind` do người/module tạo chứng từ
khai; `gl_postings.entry_kind` là **bản sao denormalize** do `PostingService`
ghi — cùng khuôn `branch_id`/`posting_date`/`period_id` đã denormalize sẵn, để
báo cáo quét bảng phát sinh không phải join header mỗi lượt.

`server_default='0'` (NGHIEP_VU) làm cột này **an toàn khi thêm vào dữ liệu đã
có**: mọi chứng từ và dòng sổ hiện hữu là phát sinh nghiệp vụ — đúng theo nghĩa
đen, vì trước lát này chưa có engine kết chuyển nào (kết chuyển thủ công thì
người dùng tự sửa lại cờ; chưa có bản cài phát hành nên chưa có ai phải sửa).

**Index có mục đích hẹp:** `ix_gl_postings_entry_kind_income` là index **một
phần**, chỉ phủ `entry_kind <> 0`. Bút toán kết chuyển là thiểu số tuyệt đối
(vài chục dòng mỗi kỳ so với hàng chục nghìn dòng nghiệp vụ), nên index đầy đủ
sẽ gần như toàn bộ bảng và không giúp gì; còn câu truy vấn thật của báo cáo
(`WHERE entry_kind = 0`) được phục vụ tốt nhất bằng chính các index sẵn có theo
`(ledger, posting_date)` — planner lọc `entry_kind` sau đó gần như miễn phí vì
nó là SMALLINT nằm cùng hàng. Index một phần này dành cho chiều ngược lại: 10a
đọc **riêng** bút toán kết chuyển để lập LCTT.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("vouchers", "gl_postings"):
        op.add_column(
            table,
            sa.Column("entry_kind", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        )
        op.create_check_constraint("entry_kind_known", table, "entry_kind BETWEEN 0 AND 1")

    op.create_index(
        "ix_gl_postings_entry_kind_income",
        "gl_postings",
        ["ledger", "period_id", "entry_kind"],
        postgresql_where=sa.text("entry_kind <> 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_gl_postings_entry_kind_income", table_name="gl_postings")
    for table in ("gl_postings", "vouchers"):
        op.drop_constraint("entry_kind_known", table, type_="check")
        op.drop_column(table, "entry_kind")
