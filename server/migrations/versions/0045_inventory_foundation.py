"""Nền kho — phiếu NK/XK/CK, sổ kho và bảy bảng tồn kho (SRS 09, lát 8A).

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0044`, bằng `ket_owner`.

Chín bảng, hai nhóm:

* **Phiếu** — `inventory_vouchers` (thân một-một với header `vouchers`, ba loại
  NK/XK/CK gom trong `kind`, bộ ba cột thủ kho `keeper_*` có mặt từ đầu để lát
  8D không migrate thân) + `inventory_voucher_lines` (cascade theo thân) — khuôn
  `cash_vouchers`/`cash_voucher_lines` (0015): không `branch_id`, RLS ở header.
* **Sổ kho** — `inventory_movements` (append-only như `gl_postings`, **có**
  `branch_id` → RLS chi nhánh, `period_id` → trigger kỳ khóa dùng lại
  `trg_block_locked_period()` của 0009: BR-STK-05 "không sửa chứng từ kho thuộc
  kỳ đã khóa"), `lots`/`serials` (LD-09: cột `lot_id`/`serial_id` có mặt trong
  MỌI bảng tồn từ migration đầu — thêm sau là migrate cả bộ), `inventory_balances`
  + `stock_layers` (8B ghi), `warehouse_book` (8D ghi, có `branch_id` → RLS),
  `inventory_recalc_queue` (8A ghi dấu, 8B đọc; có `branch_id` → RLS như
  `balance_recalc_queue`).

`lot_key`/`serial_key` = `COALESCE(lot_id, 0)` vật chất hóa: PostgreSQL không
nhận biểu thức trong `UNIQUE`, mà khóa duy nhất `(khóa tồn kho, ngày, thứ tự)`
của movement là hàng rào cuối của BR-STK-04.

Khóa ngoại THẬT tới `items`/`warehouses`/`lots` trên mọi bảng tồn (khác
`gl_postings`): `merge_service.foreign_keys_to` dời cột theo `pg_catalog`, nên
gộp hai mã hàng thì tồn kho đi theo mã đích thay vì kẹt ở id đã xóa.

Cộng hai việc ngoài kho:

* `items.min_stock_qty` (FR-SYS-047, hoãn từ 3B về đây) — ngưỡng cho guard
  FR-STK-041; hai `CHECK` mới + `group_carries_no_item_data` dựng lại để cấm
  nút nhóm mang ngưỡng (cùng lỗ "miễn chứ không cấm" mà 3B-2/7C-1 đã vá hai lần).
* **Sửa dữ liệu gói builtin TT133**: thêm TK `157` (Hàng gửi đi bán — có trong
  Phụ lục 1 TT133/2016, thiếu ở bản CSV chờ kế toán duyệt) cho gói đã gieo, để
  purpose `goods_on_consignment` mà backfill sắp thêm trỏ vào một TK có thật.
  Chỉ chèn khi gói builtin `TT133` chưa có `157` — cùng lối "chỉ đụng dòng
  builtin" của 0026.

Nghiệp vụ NK/XK/CK và các purpose mới (`finished_goods`, `work_in_progress`,
`cogs`, …) nằm trong CSV của gói; `seed._ensure_auto_posting_backfilled` lấp
theo `document_type` lúc khởi động — không chép vào đây (docstring 0028).

`_refresh_builtin_data` ở lại `0044`: lát này không đổi báo cáo hay mẫu in.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TT133_PACKAGE_CODE = "TT133-2016"
_CONSIGNMENT_ACCOUNT = "157"


def _target_schema() -> str:
    from alembic import context

    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _dataset_grantee() -> str:
    return role_name_for_schema(_target_schema())


def upgrade() -> None:
    _create_tables()
    _add_item_min_stock()
    _create_period_guard()
    _apply_security()
    _add_tt133_consignment_account()


def _create_tables() -> None:
    op.create_table(
        "inventory_vouchers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("operation_code", sa.String(length=50), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("delivered_by", sa.String(length=255), nullable=True),
        sa.Column("keeper_status", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("keeper_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keeper_posted_by", sa.Integer(), nullable=True),
        sa.Column("keeper_book_date", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f("ck_inventory_vouchers_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "(keeper_posted_at IS NULL) = (keeper_posted_by IS NULL)",
            name=op.f("ck_inventory_vouchers_keeper_stamp_complete"),
        ),
        sa.CheckConstraint(
            "(keeper_status = 1) = (keeper_book_date IS NOT NULL)",
            name=op.f("ck_inventory_vouchers_keeper_booked_has_date"),
        ),
        sa.CheckConstraint(
            "(kind = 2) = (to_warehouse_id IS NOT NULL)",
            name=op.f("ck_inventory_vouchers_transfer_has_destination"),
        ),
        sa.CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)",
            name=op.f("ck_inventory_vouchers_partner_pair_complete"),
        ),
        sa.CheckConstraint(
            "keeper_status <> 1 OR keeper_posted_at IS NOT NULL",
            name=op.f("ck_inventory_vouchers_keeper_booked_has_stamp"),
        ),
        sa.CheckConstraint(
            "keeper_status BETWEEN 0 AND 2", name=op.f("ck_inventory_vouchers_keeper_status_known")
        ),
        sa.CheckConstraint("kind BETWEEN 0 AND 2", name=op.f("ck_inventory_vouchers_kind_known")),
        sa.CheckConstraint(
            "partner_kind IS NULL OR partner_kind BETWEEN 0 AND 2",
            name=op.f("ck_inventory_vouchers_partner_kind_known"),
        ),
        sa.CheckConstraint(
            "to_warehouse_id IS NULL OR to_warehouse_id <> warehouse_id",
            name=op.f("ck_inventory_vouchers_transfer_changes_warehouse"),
        ),
        sa.ForeignKeyConstraint(
            ["id"], ["vouchers.id"], name=op.f("fk_inventory_vouchers_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_vouchers_to_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_vouchers_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_vouchers")),
    )
    op.create_index(
        "ix_inventory_vouchers_keeper_status", "inventory_vouchers", ["keeper_status"], unique=False
    )
    op.create_index(
        "ix_inventory_vouchers_warehouse", "inventory_vouchers", ["warehouse_id"], unique=False
    )
    op.create_table(
        "lots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_no", sa.String(length=50), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.CheckConstraint("lot_no <> ''", name=op.f("ck_lots_lot_no_not_blank")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_lots_item_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lots")),
        sa.UniqueConstraint("item_id", "lot_no", name="uq_lots_item_lot_no"),
    )
    op.create_table(
        "serials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("serial_no", sa.String(length=100), nullable=False),
        sa.CheckConstraint("serial_no <> ''", name=op.f("ck_serials_serial_no_not_blank")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_serials_item_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_serials")),
        sa.UniqueConstraint("item_id", "serial_no", name="uq_serials_item_serial_no"),
    )
    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("serial_id", sa.Integer(), nullable=True),
        sa.Column("lot_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column("serial_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "opening_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "opening_value", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False
        ),
        sa.Column("in_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column(
            "in_value", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False
        ),
        sa.Column("out_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column(
            "out_value", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False
        ),
        sa.Column(
            "closing_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "closing_value", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_inventory_balances_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_balances_item_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["lots.id"], name=op.f("fk_inventory_balances_lot_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["accounting_periods.id"],
            name=op.f("fk_inventory_balances_period_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["serial_id"],
            ["serials.id"],
            name=op.f("fk_inventory_balances_serial_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_balances_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_balances")),
        sa.UniqueConstraint(
            "period_id",
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            "serial_key",
            name="uq_inventory_balances_key",
        ),
    )
    op.create_table(
        "inventory_recalc_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("lot_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column(
            "marked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_inventory_recalc_queue_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_recalc_queue_item_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"],
            ["lots.id"],
            name=op.f("fk_inventory_recalc_queue_lot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_recalc_queue_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_recalc_queue")),
        sa.UniqueConstraint(
            "branch_id", "warehouse_id", "item_id", "lot_key", name="uq_inventory_recalc_queue_key"
        ),
    )
    op.create_table(
        "inventory_voucher_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("item_variant_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("serial_id", sa.Integer(), nullable=True),
        sa.Column("unit_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("base_quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_cost_fc", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("amount_fc", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("debit_account_id", sa.Integer(), nullable=True),
        sa.Column("credit_account_id", sa.Integer(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("extended_dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_line_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(debit_account_id IS NULL) = (credit_account_id IS NULL)",
            name=op.f("ck_inventory_voucher_lines_account_pair_complete"),
        ),
        sa.CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)",
            name=op.f("ck_inventory_voucher_lines_partner_pair_complete"),
        ),
        sa.CheckConstraint(
            "amount_fc IS NULL OR amount_fc >= 0",
            name=op.f("ck_inventory_voucher_lines_amount_not_negative"),
        ),
        sa.CheckConstraint(
            "base_quantity > 0", name=op.f("ck_inventory_voucher_lines_base_quantity_positive")
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_inventory_voucher_lines_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_cost_fc IS NULL OR unit_cost_fc >= 0",
            name=op.f("ck_inventory_voucher_lines_unit_cost_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["credit_account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_inventory_voucher_lines_credit_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["debit_account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_inventory_voucher_lines_debit_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_voucher_lines_item_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"],
            ["lots.id"],
            name=op.f("fk_inventory_voucher_lines_lot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["serial_id"],
            ["serials.id"],
            name=op.f("fk_inventory_voucher_lines_serial_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["units_of_measure.id"],
            name=op.f("fk_inventory_voucher_lines_unit_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["inventory_vouchers.id"],
            name=op.f("fk_inventory_voucher_lines_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_voucher_lines_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_voucher_lines")),
    )
    op.create_index(
        "ix_inventory_voucher_lines_source_line",
        "inventory_voucher_lines",
        ["source_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_voucher_lines_voucher",
        "inventory_voucher_lines",
        ["voucher_id", "line_no"],
        unique=False,
    )
    op.create_table(
        "warehouse_book",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("book_date", sa.Date(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("in_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("out_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("posted_by", sa.Integer(), nullable=False),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "in_qty >= 0 AND out_qty >= 0", name=op.f("ck_warehouse_book_quantities_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_warehouse_book_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_warehouse_book_item_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["lots.id"], name=op.f("fk_warehouse_book_lot_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f("fk_warehouse_book_voucher_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_warehouse_book_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouse_book")),
    )
    op.create_index(
        "ix_warehouse_book_key",
        "warehouse_book",
        ["warehouse_id", "item_id", "book_date"],
        unique=False,
    )
    op.create_index("ix_warehouse_book_voucher", "warehouse_book", ["voucher_id"], unique=False)
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("item_variant_id", sa.Integer(), nullable=True),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("serial_id", sa.Integer(), nullable=True),
        sa.Column("lot_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_custodial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("sequence_in_day", sa.Integer(), nullable=False),
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("cost_state", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("source_movement_id", sa.BigInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(cost_state = 0) = (unit_cost IS NULL)",
            name=op.f("ck_inventory_movements_costed_has_unit_cost"),
        ),
        sa.CheckConstraint(
            "(unit_cost IS NULL) = (amount IS NULL)",
            name=op.f("ck_inventory_movements_cost_pair_complete"),
        ),
        sa.CheckConstraint(
            "cost_state BETWEEN 0 AND 2", name=op.f("ck_inventory_movements_cost_state_known")
        ),
        sa.CheckConstraint(
            "direction IN (-1, 1)", name=op.f("ck_inventory_movements_direction_known")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_inventory_movements_quantity_positive")),
        sa.CheckConstraint(
            "sequence_in_day > 0", name=op.f("ck_inventory_movements_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_inventory_movements_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_movements_item_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["inventory_voucher_lines.id"],
            name=op.f("fk_inventory_movements_line_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["lots.id"], name=op.f("fk_inventory_movements_lot_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["accounting_periods.id"],
            name=op.f("fk_inventory_movements_period_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["serial_id"],
            ["serials.id"],
            name=op.f("fk_inventory_movements_serial_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f("fk_inventory_movements_voucher_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_movements_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_movements")),
        sa.UniqueConstraint(
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            "posting_date",
            "sequence_in_day",
            name="uq_inventory_movements_day_sequence",
        ),
    )
    op.create_index(
        "ix_inventory_movements_needs_cost",
        "inventory_movements",
        ["cost_state"],
        unique=False,
        postgresql_where=sa.text("cost_state <> 1"),
    )
    op.create_index(
        "ix_inventory_movements_period", "inventory_movements", ["period_id"], unique=False
    )
    op.create_index(
        "ix_inventory_movements_voucher", "inventory_movements", ["voucher_id"], unique=False
    )
    op.create_table(
        "stock_layers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("movement_id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("lot_key", sa.Integer(), server_default="0", nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("sequence_in_day", sa.Integer(), nullable=False),
        sa.Column("original_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("remaining_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.CheckConstraint("original_qty > 0", name=op.f("ck_stock_layers_original_qty_positive")),
        sa.CheckConstraint(
            "remaining_qty >= 0 AND remaining_qty <= original_qty",
            name=op.f("ck_stock_layers_remaining_within_original"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_stock_layers_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_stock_layers_item_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["lots.id"], name=op.f("fk_stock_layers_lot_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["movement_id"],
            ["inventory_movements.id"],
            name=op.f("fk_stock_layers_movement_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_layers_warehouse_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_layers")),
    )
    op.create_index(
        "ix_stock_layers_open",
        "stock_layers",
        ["branch_id", "warehouse_id", "item_id", "lot_key", "receipt_date", "sequence_in_day"],
        unique=False,
        postgresql_where=sa.text("remaining_qty > 0"),
    )


def _add_item_min_stock() -> None:
    """FR-SYS-047: ngưỡng tồn tối thiểu + cấm nút nhóm mang nó."""
    op.add_column(
        "items", sa.Column("min_stock_qty", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_items_min_stock_needs_stock_nature"),
        "items",
        "min_stock_qty IS NULL OR nature IN ('finished_goods', 'goods')",
    )
    op.create_check_constraint(
        op.f("ck_items_min_stock_not_negative"),
        "items",
        "min_stock_qty IS NULL OR min_stock_qty >= 0",
    )
    op.drop_constraint(op.f("ck_items_group_carries_no_item_data"), "items", type_="check")
    op.create_check_constraint(
        op.f("ck_items_group_carries_no_item_data"),
        "items",
        "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL "
        "AND warehouse_id IS NULL AND price_is_tax_inclusive IS NULL "
        "AND min_stock_qty IS NULL)",
    )


def _create_period_guard() -> None:
    """BR-STK-05: kỳ khóa thì sổ kho bất động — dùng lại hàm trigger của 0009
    (`COALESCE(NEW.period_id, OLD.period_id)`), thêm cả UPDATE vì engine 8B
    ghi giá vào dòng đã có và lượt sắp xếp lại đổi `sequence_in_day`."""
    op.execute(
        """
        CREATE TRIGGER inventory_movements_period_guard
          BEFORE INSERT OR UPDATE OR DELETE ON inventory_movements
          FOR EACH ROW EXECUTE FUNCTION trg_block_locked_period()
        """
    )


def _apply_security() -> None:
    """Quyền cho vai trò runtime + RLS chi nhánh cho ba bảng mang `branch_id`.

    Bảng `SERIAL`/`BIGSERIAL` cần thêm quyền trên sequence (0015). Thân/dòng
    phiếu không `branch_id` — phạm vi là của header `vouchers`, cùng lối 0026;
    `inventory_balances`/`stock_layers` mang `branch_id` nhưng là bảng dẫn xuất
    do job per-branch (8B) ghi — bật RLS luôn để lượt đọc lưới tồn của người
    dùng không thấy chi nhánh khác.
    """
    grantee = _dataset_grantee()
    for table, sequence in (
        ("inventory_vouchers", None),
        ("inventory_voucher_lines", None),
        ("lots", serial_sequence_name("lots")),
        ("serials", serial_sequence_name("serials")),
        ("inventory_movements", serial_sequence_name("inventory_movements")),
        ("inventory_balances", serial_sequence_name("inventory_balances")),
        ("stock_layers", serial_sequence_name("stock_layers")),
        ("warehouse_book", serial_sequence_name("warehouse_book")),
        ("inventory_recalc_queue", serial_sequence_name("inventory_recalc_queue")),
    ):
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)
    for table in (
        "inventory_movements",
        "inventory_balances",
        "stock_layers",
        "warehouse_book",
        "inventory_recalc_queue",
    ):
        for statement in enable_branch_rls_statements(table, allow_null_branch=False):
            op.execute(statement)


def _add_tt133_consignment_account() -> None:
    """TK 157 cho gói builtin TT133 đã gieo mà chưa có — xem docstring đầu tệp.

    `path` = `<id>.` và `level` = 1 như một TK cấp 1 do `_insert_accounts` gieo;
    chèn rồi cập nhật `path` vì `id` chỉ có sau INSERT.
    """
    op.execute(
        sa.text(
            "INSERT INTO chart_of_accounts "
            "(package_id, code, name, name_en, parent_id, path, level, balance_nature, "
            " is_summary, is_foreign_currency, detail_tracking, is_locked, is_inactive) "
            "SELECT p.id, :code, 'Hàng gửi đi bán', NULL, NULL, '0.', 1, 0, "
            "       false, false, ARRAY['item','warehouse']::varchar(20)[], true, false "
            "  FROM config_packages p "
            " WHERE p.code = :package "
            "   AND p.is_builtin "
            "   AND NOT EXISTS (SELECT 1 FROM chart_of_accounts c "
            "                    WHERE c.package_id = p.id AND c.code = :code)"
        ).bindparams(code=_CONSIGNMENT_ACCOUNT, package=_TT133_PACKAGE_CODE)
    )
    op.execute(
        sa.text(
            "UPDATE chart_of_accounts SET path = id::text || '.' "
            " WHERE code = :code AND path = '0.'"
        ).bindparams(code=_CONSIGNMENT_ACCOUNT)
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS inventory_movements_period_guard ON inventory_movements")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON inventory_recalc_queue")
    op.execute("ALTER TABLE inventory_recalc_queue DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON warehouse_book")
    op.execute("ALTER TABLE warehouse_book DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON stock_layers")
    op.execute("ALTER TABLE stock_layers DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON inventory_balances")
    op.execute("ALTER TABLE inventory_balances DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON inventory_movements")
    op.execute("ALTER TABLE inventory_movements DISABLE ROW LEVEL SECURITY")
    op.drop_table("stock_layers")
    op.drop_table("inventory_movements")
    op.drop_table("warehouse_book")
    op.drop_table("inventory_voucher_lines")
    op.drop_table("inventory_recalc_queue")
    op.drop_table("inventory_balances")
    op.drop_table("serials")
    op.drop_table("lots")
    op.drop_table("inventory_vouchers")
    op.drop_constraint(op.f("ck_items_group_carries_no_item_data"), "items", type_="check")
    op.create_check_constraint(
        op.f("ck_items_group_carries_no_item_data"),
        "items",
        "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL "
        "AND warehouse_id IS NULL AND price_is_tax_inclusive IS NULL)",
    )
    op.drop_constraint(op.f("ck_items_min_stock_not_negative"), "items", type_="check")
    op.drop_constraint(op.f("ck_items_min_stock_needs_stock_nature"), "items", type_="check")
    op.drop_column("items", "min_stock_qty")
