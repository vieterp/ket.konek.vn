"""Lắp ráp / tháo dỡ, định mức NVL, đích danh trên dòng bán (lát 8C-2).

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0047`, bằng `ket_owner`.

Bốn việc, một revision:

* **`inventory_vouchers`**: `CHECK kind_known` mở tới 4 (3 = lắp ráp LR, 4 =
  tháo dỡ TD, FR-STK-005/016) + chỉ mục `kind` cho hai câu vế của engine.
* **`inventory_voucher_lines`**: `is_product` (dòng thành phẩm của LR/TD) +
  `allocation_ratio` (tỷ lệ phân bổ giá trị khi tháo dỡ) + chỉ mục duy nhất một
  phần "≤ 1 dòng thành phẩm mỗi phiếu". Bảng dựng ở 0045 chưa có dữ liệu thật
  nên `ALTER` là rẻ.
* **`item_bom_lines`** (FR-SYS-044) — bảng con của `items`, khuôn `item_units`:
  linh kiện + số lượng cho một đơn vị chính thành phẩm + tỷ lệ phân bổ.
* **`sales_invoice_lines.source_movement_id`** — lần nhập đích danh người bán
  chọn (phương pháp 4); không FK, module kho kiểm khi sinh phiếu.

Dữ liệu gói: hai nghiệp vụ `LR,lap-rap` / `TD,thao-do` vào `auto_posting_rules.csv`
được `seed._ensure_auto_posting_backfilled` chèn theo `document_type` mới lúc
khởi động — không bump `version` gói (bump sẽ chặn backfill). `_refresh_builtin_
data` GIỮ ở `0044`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VOUCHERS = "inventory_vouchers"
_LINES = "inventory_voucher_lines"
_BOM = "item_bom_lines"

_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6
_RATIO_PRECISION = 18
_RATIO_SCALE = 6
"""Bằng `kernel/quantity.py` và `ALLOCATION_RATIO_*` của model — viết ra vì
migration không đi theo mã nguồn về sau."""


def _target_schema() -> str:
    from alembic import context

    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    _open_voucher_kinds()
    _add_assembly_line_columns()
    _create_bom_lines()
    op.add_column(
        "sales_invoice_lines", sa.Column("source_movement_id", sa.BigInteger(), nullable=True)
    )


def _open_voucher_kinds() -> None:
    # Tên **trần**: quy ước `op.f` tự thêm `ck_inventory_vouchers_` (xem 0027).
    op.drop_constraint("kind_known", _VOUCHERS, type_="check")
    op.create_check_constraint("kind_known", _VOUCHERS, "kind BETWEEN 0 AND 4")
    op.create_index(f"ix_{_VOUCHERS}_kind", _VOUCHERS, ["kind"], unique=False)


def _add_assembly_line_columns() -> None:
    op.add_column(
        _LINES,
        sa.Column("is_product", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        _LINES,
        sa.Column(
            "allocation_ratio",
            sa.Numeric(precision=_RATIO_PRECISION, scale=_RATIO_SCALE),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "allocation_ratio_positive", _LINES, "allocation_ratio IS NULL OR allocation_ratio > 0"
    )
    op.create_index(
        f"uq_{_LINES}_product",
        _LINES,
        ["voucher_id"],
        unique=True,
        postgresql_where=sa.text("is_product"),
    )


def _create_bom_lines() -> None:
    table = _BOM
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("component_item_id", sa.Integer(), nullable=False),
        sa.Column(
            "quantity",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=False,
        ),
        sa.Column(
            "allocation_ratio",
            sa.Numeric(precision=_RATIO_PRECISION, scale=_RATIO_SCALE),
            nullable=False,
            server_default="1",
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "item_id <> component_item_id", name=op.f(f"ck_{table}_component_differs")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f(f"ck_{table}_quantity_positive")),
        sa.CheckConstraint(
            "allocation_ratio > 0", name=op.f(f"ck_{table}_allocation_ratio_positive")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{table}_item_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_item_id"],
            ["items.id"],
            name=op.f(f"fk_{table}_component_item_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("item_id", "component_item_id", name=f"uq_{table}_item_component"),
    )
    op.create_index(f"ix_{table}_item_id", table, ["item_id"])
    op.create_index(f"ix_{table}_component_item_id", table, ["component_item_id"])
    grantee = role_name_for_schema(_target_schema())
    for statement in grant_read_write(
        table, grantee=grantee, sequence=serial_sequence_name(table, "id")
    ):
        op.execute(statement)


def downgrade() -> None:
    op.drop_column("sales_invoice_lines", "source_movement_id")
    op.drop_index(f"ix_{_BOM}_component_item_id", table_name=_BOM)
    op.drop_index(f"ix_{_BOM}_item_id", table_name=_BOM)
    op.drop_table(_BOM)
    op.drop_index(f"uq_{_LINES}_product", table_name=_LINES)
    op.drop_constraint("allocation_ratio_positive", _LINES, type_="check")
    op.drop_column(_LINES, "allocation_ratio")
    op.drop_column(_LINES, "is_product")
    op.drop_index(f"ix_{_VOUCHERS}_kind", table_name=_VOUCHERS)
    op.drop_constraint("kind_known", _VOUCHERS, type_="check")
    op.create_check_constraint("kind_known", _VOUCHERS, "kind BETWEEN 0 AND 2")
