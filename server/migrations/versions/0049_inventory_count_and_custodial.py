"""Kiểm kê kho, hàng giữ hộ, sổ kho thủ kho (lát 8D).

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-23

Chạy **một lần cho mỗi schema dataset** như `0001`..`0048`, bằng `ket_owner`.

Ba việc, một revision:

* **Hàng giữ hộ** (BR-STK-07): `inventory_voucher_lines.is_custodial` (cột trên
  `inventory_movements` đã có từ 0045, 8A đặt sẵn) + nới `cost_state` lên 3
  (`NOT_APPLICABLE` — hàng giữ hộ không có giá và sẽ không bao giờ có) + sửa hai
  ràng buộc đi kèm: `costed_has_unit_cost` cho 3 đứng cùng vế NULL với 0, và
  ràng buộc mới `custodial_has_no_cost` khóa chiều ngược lại. Chỉ mục riêng
  phần `ix_inventory_movements_needs_cost` đổi điều kiện sang `NOT IN (1, 3)`
  để dòng giữ hộ không phình một chỉ mục dựng ra để tìm việc cho engine.
* **Kiểm kê kho** (FR-STK-030/031): `inventory_count_sheets` +
  `inventory_count_sheet_lines`, khuôn `cash_count_sheets` của 6B.
* Không đụng `warehouse_book` — bảng dựng rỗng từ 0045, lát này chỉ bắt đầu ghi.

Dữ liệu gói: **không** thêm dòng nào vào `auto_posting_rules.csv`. Phiếu xử lý
chênh lệch kiểm kê dùng nghiệp vụ `nhap-khac`/`xuat-khac` đã có từ 8A và tự điền
cặp TK từ `default_accounts` (`unexplained_surplus`/`unexplained_shortage`, khai
ở `document_type = '*'` từ phase 6). Lý do: `seed._ensure_auto_posting_backfilled`
vá nghiệp vụ **theo từng `document_type`**, mà NK/XK đã có dòng — một nghiệp vụ
mới cho chúng sẽ không bao giờ tới được dataset gieo trước lát này.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINES = "inventory_voucher_lines"
_MOVEMENTS = "inventory_movements"
_SHEETS = "inventory_count_sheets"
_SHEET_LINES = "inventory_count_sheet_lines"

_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6
_UNIT_COST_PRECISION = 18
_UNIT_COST_SCALE = 6
"""Bằng `kernel/quantity.py` và `UNIT_COST_*` của model — viết ra vì migration
không đi theo mã nguồn về sau."""

_NOTE_MAX = 500
_SHEET_NO_MAX = 50


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
    _add_custodial_line_column()
    _open_cost_state_for_custodial()
    _create_count_sheets()


def _add_custodial_line_column() -> None:
    op.add_column(
        _LINES,
        sa.Column("is_custodial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def _open_cost_state_for_custodial() -> None:
    # Tên **trần**: quy ước `op.f` tự thêm `ck_inventory_movements_` (xem 0027).
    op.drop_constraint("cost_state_known", _MOVEMENTS, type_="check")
    op.create_check_constraint("cost_state_known", _MOVEMENTS, "cost_state BETWEEN 0 AND 3")
    op.drop_constraint("costed_has_unit_cost", _MOVEMENTS, type_="check")
    op.create_check_constraint(
        "costed_has_unit_cost", _MOVEMENTS, "(cost_state IN (0, 3)) = (unit_cost IS NULL)"
    )
    op.create_check_constraint(
        "custodial_has_no_cost", _MOVEMENTS, "NOT is_custodial OR cost_state = 3"
    )
    op.drop_index("ix_inventory_movements_needs_cost", table_name=_MOVEMENTS)
    op.create_index(
        "ix_inventory_movements_needs_cost",
        _MOVEMENTS,
        ["cost_state"],
        unique=False,
        postgresql_where=sa.text("cost_state NOT IN (1, 3)"),
    )


def _create_count_sheets() -> None:
    op.create_table(
        _SHEETS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("sheet_no", sa.String(length=_SHEET_NO_MAX), nullable=False),
        sa.Column("count_date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=_NOTE_MAX), nullable=True),
        sa.Column("adjustment_receipt_id", sa.Uuid(), nullable=True),
        sa.Column("adjustment_issue_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{_SHEETS}_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f(f"fk_{_SHEETS}_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_receipt_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{_SHEETS}_adjustment_receipt_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_issue_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{_SHEETS}_adjustment_issue_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_SHEETS}")),
    )
    op.create_index(
        f"ix_{_SHEETS}_warehouse_date", _SHEETS, ["warehouse_id", "count_date"], unique=False
    )
    op.create_table(
        _SHEET_LINES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sheet_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("lot_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_custodial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "book_qty",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=False,
        ),
        sa.Column(
            "counted_qty",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=True,
        ),
        sa.Column(
            "unit_cost",
            sa.Numeric(precision=_UNIT_COST_PRECISION, scale=_UNIT_COST_SCALE),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=_NOTE_MAX), nullable=True),
        sa.CheckConstraint(
            "counted_qty IS NULL OR counted_qty >= 0",
            name=op.f(f"ck_{_SHEET_LINES}_counted_qty_not_negative"),
        ),
        sa.CheckConstraint(
            "unit_cost IS NULL OR unit_cost >= 0",
            name=op.f(f"ck_{_SHEET_LINES}_unit_cost_not_negative"),
        ),
        sa.CheckConstraint(
            "NOT is_custodial OR unit_cost IS NULL",
            name=op.f(f"ck_{_SHEET_LINES}_custodial_line_has_no_cost"),
        ),
        sa.ForeignKeyConstraint(
            ["sheet_id"],
            [f"{_SHEETS}.id"],
            name=op.f(f"fk_{_SHEET_LINES}_sheet_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{_SHEET_LINES}_item_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["lots.id"], name=op.f(f"fk_{_SHEET_LINES}_lot_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_SHEET_LINES}")),
        sa.UniqueConstraint(
            "sheet_id", "item_id", "lot_key", "is_custodial", name="uq_inventory_count_sheet_key"
        ),
    )
    op.create_index(f"ix_{_SHEET_LINES}_sheet", _SHEET_LINES, ["sheet_id", "line_no"], unique=False)
    grantee = role_name_for_schema(_target_schema())
    for table in (_SHEETS, _SHEET_LINES):
        for statement in grant_read_write(table, grantee=grantee, sequence=None):
            op.execute(statement)
    # Chỉ thân biên bản mang `branch_id` → RLS chi nhánh; dòng lấy phạm vi từ
    # thân qua `sheet_id` CASCADE, cùng lối dòng phiếu của 0045.
    for statement in enable_branch_rls_statements(_SHEETS, allow_null_branch=False):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(f"ix_{_SHEET_LINES}_sheet", table_name=_SHEET_LINES)
    op.drop_table(_SHEET_LINES)
    op.drop_index(f"ix_{_SHEETS}_warehouse_date", table_name=_SHEETS)
    op.drop_table(_SHEETS)
    op.drop_index("ix_inventory_movements_needs_cost", table_name=_MOVEMENTS)
    op.create_index(
        "ix_inventory_movements_needs_cost",
        _MOVEMENTS,
        ["cost_state"],
        unique=False,
        postgresql_where=sa.text("cost_state <> 1"),
    )
    op.drop_constraint("custodial_has_no_cost", _MOVEMENTS, type_="check")
    op.drop_constraint("costed_has_unit_cost", _MOVEMENTS, type_="check")
    op.create_check_constraint(
        "costed_has_unit_cost", _MOVEMENTS, "(cost_state = 0) = (unit_cost IS NULL)"
    )
    op.drop_constraint("cost_state_known", _MOVEMENTS, type_="check")
    op.create_check_constraint("cost_state_known", _MOVEMENTS, "cost_state BETWEEN 0 AND 2")
    op.drop_column(_LINES, "is_custodial")
