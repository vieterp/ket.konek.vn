"""Đích danh: dòng phiếu xuất trỏ lần nhập (SRS 09 §3 phương pháp 4, lát 8B).

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0045`, bằng `ket_owner`.

Một cột: `inventory_voucher_lines.source_movement_id` → `inventory_movements.id`
(`RESTRICT`). Movement đã có `source_movement_id` từ 0045 nhưng dòng phiếu — thứ
người dùng gõ — chưa có chỗ để chỉ "xuất từ lần nhập nào"; engine tính giá đích
danh đọc cột trên movement, còn cột này là nguồn của nó lúc ghi sổ. Bảng tồn kho
không đổi hình (LD-09 chỉ nói về khóa tồn kho; đây là dòng chứng từ).

Không RLS mới (dòng phiếu đi theo header như 0045), không đổi dữ liệu gói, không
đụng `_refresh_builtin_data`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_voucher_lines",
        sa.Column("source_movement_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_inventory_voucher_lines_source_movement_id_inventory_movements"),
        "inventory_voucher_lines",
        "inventory_movements",
        ["source_movement_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_inventory_voucher_lines_source_movement",
        "inventory_voucher_lines",
        ["source_movement_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_voucher_lines_source_movement", table_name="inventory_voucher_lines"
    )
    op.drop_constraint(
        op.f("fk_inventory_voucher_lines_source_movement_id_inventory_movements"),
        "inventory_voucher_lines",
        type_="foreignkey",
    )
    op.drop_column("inventory_voucher_lines", "source_movement_id")
