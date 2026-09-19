"""Tồn đầu kỳ theo lớp, phạm vi bình quân, hàng bán trả lại lấy giá xuất (lát 8C-1).

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0046`, bằng `ket_owner`.

Bốn việc, một revision:

* **`opening_balance_stock_layers`** (RT-24, FR-OPB-004) — bảng con của dòng
  `opening_balances` nhóm 5, mỗi dòng một lần nhập; `branch_id` + RLS như
  `opening_balance_invoices` (0025).
* **`inventory_movements`**: `voucher_id`/`line_id` thành NULL được +
  `opening_layer_id` (FK `RESTRICT` về lớp) + hai `CHECK` loại trừ — một movement
  hoặc thuộc một dòng phiếu, hoặc là một lớp tồn đầu kỳ. Chỉ mục mới
  `(branch_id, item_id, lot_key, posting_date, sequence_in_day, warehouse_id, id)`
  cho bước LATERAL "không theo kho" (FR-STK-007). Bảng vừa dựng ở 0045/0046 và
  chưa có dữ liệu thật (v1 phát hành sau phase 11) nên `ALTER` là rẻ — LD-09
  nói về bảng đã có tồn kho.
* **`fiscal_years.inventory_costing_by_warehouse`** (FR-STK-007), mặc định `true`
  — năm đã tạo giữ nghĩa cũ (theo kho).
* **`sales_invoice_lines.returned_line_id`** (FR-STK-004) — dòng bán gốc của
  dòng hàng-bán-trả-lại, `RESTRICT`.

Không đổi dữ liệu gói, không đụng `_refresh_builtin_data`. `downgrade()` giả
định chưa có movement lớp đầu kỳ (cột về NOT NULL) — đúng với dataset dev/test.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LAYERS = "opening_balance_stock_layers"
_MOVEMENTS = "inventory_movements"


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
    _create_stock_layers()
    _open_movements_to_opening_layers()
    op.add_column(
        "fiscal_years",
        sa.Column(
            "inventory_costing_by_warehouse",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column("sales_invoice_lines", sa.Column("returned_line_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_sales_invoice_lines_returned_line_id"),
        "sales_invoice_lines",
        "sales_invoice_lines",
        ["returned_line_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_sales_invoice_lines_returned_line",
        "sales_invoice_lines",
        ["returned_line_id"],
        unique=False,
    )


def _create_stock_layers() -> None:
    op.create_table(
        _LAYERS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opening_balance_id", sa.BigInteger(), nullable=False),
        sa.Column("fiscal_year_id", sa.Integer(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("received_on", sa.Date(), nullable=True),
        sa.Column("receipt_no", sa.String(length=50), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f(f"ck_{_LAYERS}_quantity_positive")),
        sa.CheckConstraint(
            "unit_cost >= 0 AND amount >= 0", name=op.f(f"ck_{_LAYERS}_cost_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["opening_balance_id"],
            ["opening_balances.id"],
            name=op.f(f"fk_{_LAYERS}_opening_balance_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fiscal_year_id"],
            ["fiscal_years.id"],
            name=op.f(f"fk_{_LAYERS}_fiscal_year_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_LAYERS}")),
    )
    op.create_index(f"ix_{_LAYERS}_parent", _LAYERS, ["opening_balance_id"], unique=False)
    op.create_index(f"ix_{_LAYERS}_branch", _LAYERS, ["branch_id"], unique=False)
    grantee = role_name_for_schema(_target_schema())
    for statement in grant_read_write(_LAYERS, grantee=grantee, sequence=None):
        op.execute(statement)
    for statement in enable_branch_rls_statements(_LAYERS, allow_null_branch=False):
        op.execute(statement)


def _open_movements_to_opening_layers() -> None:
    op.alter_column(_MOVEMENTS, "voucher_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column(_MOVEMENTS, "line_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(_MOVEMENTS, sa.Column("opening_layer_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{_MOVEMENTS}_opening_layer_id"),
        _MOVEMENTS,
        _LAYERS,
        ["opening_layer_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f(f"ck_{_MOVEMENTS}_voucher_line_pair_complete"),
        _MOVEMENTS,
        "(voucher_id IS NULL) = (line_id IS NULL)",
    )
    op.create_check_constraint(
        op.f(f"ck_{_MOVEMENTS}_opening_layer_xor_voucher"),
        _MOVEMENTS,
        "(voucher_id IS NULL) = (opening_layer_id IS NOT NULL)",
    )
    op.create_index(f"ix_{_MOVEMENTS}_opening_layer", _MOVEMENTS, ["opening_layer_id"], unique=True)
    op.create_index(
        f"ix_{_MOVEMENTS}_branch_item_order",
        _MOVEMENTS,
        [
            "branch_id",
            "item_id",
            "lot_key",
            "posting_date",
            "sequence_in_day",
            "warehouse_id",
            "id",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sales_invoice_lines_returned_line", table_name="sales_invoice_lines")
    op.drop_constraint(
        op.f("fk_sales_invoice_lines_returned_line_id"), "sales_invoice_lines", type_="foreignkey"
    )
    op.drop_column("sales_invoice_lines", "returned_line_id")
    op.drop_column("fiscal_years", "inventory_costing_by_warehouse")
    op.drop_index(f"ix_{_MOVEMENTS}_branch_item_order", table_name=_MOVEMENTS)
    op.drop_index(f"ix_{_MOVEMENTS}_opening_layer", table_name=_MOVEMENTS)
    op.drop_constraint(
        op.f(f"ck_{_MOVEMENTS}_opening_layer_xor_voucher"), _MOVEMENTS, type_="check"
    )
    op.drop_constraint(
        op.f(f"ck_{_MOVEMENTS}_voucher_line_pair_complete"), _MOVEMENTS, type_="check"
    )
    op.drop_constraint(op.f(f"fk_{_MOVEMENTS}_opening_layer_id"), _MOVEMENTS, type_="foreignkey")
    op.drop_column(_MOVEMENTS, "opening_layer_id")
    op.alter_column(_MOVEMENTS, "line_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column(_MOVEMENTS, "voucher_id", existing_type=sa.Uuid(), nullable=False)
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON opening_balance_stock_layers")
    op.execute("ALTER TABLE opening_balance_stock_layers DISABLE ROW LEVEL SECURITY")
    op.drop_table(_LAYERS)
