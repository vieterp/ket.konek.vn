"""Hóa đơn bán hàng — ba bảng của module `sales`.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-04

Chạy **một lần cho mỗi schema dataset** như `0001`..`0027`, bằng `ket_owner`.

Ba bảng, cùng khuôn `purchase_invoices`/`purchase_invoice_lines`/
`purchase_settlements` (0026): thân một-một với header `vouchers`, dòng con
`CASCADE` theo thân, không bảng nào mang `branch_id` — phạm vi chi nhánh là
của header và RLS canh ở đó, nên ở đây chỉ cấp quyền, không bật RLS.

* `sales_invoices` — thân hóa đơn bán (năm nghiệp vụ gom trong `kind`).
* `sales_invoice_lines` — dòng hàng/dịch vụ: TK doanh thu bên Có, chiết khấu
  thương mại, thuế GTGT đầu ra, ba cột giá vốn phase 8 điền, đủ bộ chiều phân
  tích, và hai cột truy nguyên nguồn giá.
* `sales_settlements` — đối trừ của chứng từ trả lại / giảm giá vào hóa đơn gốc.

Cộng **một bước làm mới metadata builtin**: báo cáo `tuoi-no-phai-thu` đổi
`required_permission_module` từ `receivables` sang `sales`. Từ 7A hai chiều tuổi
nợ dùng chung mã quyền `receivables` vì chưa phân hệ nào của chúng tồn tại;
chiều phải trả đã chuyển sang `purchase` ở 0026, và lát này đóng nốt chiều phải
thu. Bước làm mới phải đứng ở **cuối** chuỗi migration mỗi khi chuỗi thêm thứ
mà dữ liệu builtin đọc (doctrine ghi ở 0025); 0027 không đổi metadata nào nên
nó không có bước này, và đây là chỗ đúng cho lượt đổi lần này.

**Không có nghiệp vụ định khoản `SAL` ở đây.** Bộ nghiệp vụ và các purpose mới
(`revenue_goods`, `revenue_services`, `sales_deduction`, `cogs`) nằm trong tệp
CSV của gói dựng sẵn, và `seed._ensure_auto_posting_backfilled` lấp chúng vào
dataset đã gieo — nó tính chỗ trống **theo từng `document_type`**, chính vì
những lát như lát này. Chép chúng vào migration là để hai đường gieo cùng một
dữ liệu, và khi chúng lệch nhau thì không ai biết đường nào đúng.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6
_UNIT_PRICE_PRECISION = 24
_UNIT_PRICE_SCALE = 6
_VAT_RATE_PRECISION = 5
_VAT_RATE_SCALE = 2
_DISCOUNT_PERCENT_PRECISION = 5
_DISCOUNT_PERCENT_SCALE = 2

_OPERATION_CODE_MAX_LENGTH = 50
_DESCRIPTION_MAX_LENGTH = 500
_INVOICE_FORM_MAX_LENGTH = 20
_INVOICE_SERIAL_MAX_LENGTH = 20
_INVOICE_NO_MAX_LENGTH = 50
_SHIP_TO_MAX_LENGTH = 500
_RECIPIENT_MAX_LENGTH = 200
_PRICE_SOURCE_MAX_LENGTH = 20

_TABLES = ("sales_invoices", "sales_invoice_lines", "sales_settlements")


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _dataset_grantee() -> str:
    return role_name_for_schema(_target_schema())


def _amount(name: str, *, default_zero: bool = False) -> sa.Column[Decimal]:
    return sa.Column(
        name,
        sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
        nullable=False,
        server_default="0" if default_zero else None,
    )


def _account_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        ["chart_of_accounts.id"],
        name=op.f(f"fk_{table}_{column}"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    _create_sales_invoices()
    _create_sales_invoice_lines()
    _create_sales_settlements()
    _apply_security()
    _refresh_builtin_data()


def _create_sales_invoices() -> None:
    table = "sales_invoices"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        # `SalesInvoiceKind`: 0 bán hàng hóa, 1 bán dịch vụ, 2 hàng bán trả
        # lại, 3 giảm giá hàng bán, 4 bán qua đại lý — số trần có chú thích,
        # không import enum (migration là ảnh chụp lịch sử, 0022 nêu cùng luật).
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("operation_code", sa.String(length=_OPERATION_CODE_MAX_LENGTH), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("salesperson_id", sa.Integer(), nullable=True),
        sa.Column("ship_to", sa.String(length=_SHIP_TO_MAX_LENGTH), nullable=True),
        sa.Column("recipient_name", sa.String(length=_RECIPIENT_MAX_LENGTH), nullable=True),
        sa.Column("invoice_form", sa.String(length=_INVOICE_FORM_MAX_LENGTH), nullable=True),
        sa.Column("invoice_serial", sa.String(length=_INVOICE_SERIAL_MAX_LENGTH), nullable=True),
        sa.Column("invoice_no", sa.String(length=_INVOICE_NO_MAX_LENGTH), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("payment_term_id", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("receivable_account_id", sa.Integer(), nullable=False),
        sa.Column("price_list_id", sa.Integer(), nullable=True),
        sa.Column("is_stock_issue", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cogs_posted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _amount("total_before_tax_fc"),
        _amount("total_discount_fc"),
        _amount("total_vat_fc"),
        _amount("total_fc"),
        sa.CheckConstraint("kind BETWEEN 0 AND 4", name=op.f(f"ck_{table}_kind_known")),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f(f"ck_{table}_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "total_before_tax_fc >= 0 AND total_discount_fc >= 0 "
            "AND total_vat_fc >= 0 AND total_fc >= 0",
            name=op.f(f"ck_{table}_totals_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["id"], ["vouchers.id"], name=op.f(f"fk_{table}_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["payment_term_id"],
            ["payment_terms.id"],
            name=op.f(f"fk_{table}_payment_term_id"),
            ondelete="RESTRICT",
        ),
        _account_fk(table, "receivable_account_id"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_customer", table, ["customer_id"])


def _create_sales_invoice_lines() -> None:
    table = "sales_invoice_lines"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=_DESCRIPTION_MAX_LENGTH), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column(
            "quantity",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=True,
        ),
        sa.Column(
            "unit_price_fc",
            sa.Numeric(precision=_UNIT_PRICE_PRECISION, scale=_UNIT_PRICE_SCALE),
            nullable=True,
        ),
        sa.Column(
            "discount_percent",
            sa.Numeric(precision=_DISCOUNT_PERCENT_PRECISION, scale=_DISCOUNT_PERCENT_SCALE),
            nullable=True,
        ),
        _amount("discount_amount_fc", default_zero=True),
        _amount("amount_fc"),
        sa.Column(
            "vat_rate",
            sa.Numeric(precision=_VAT_RATE_PRECISION, scale=_VAT_RATE_SCALE),
            nullable=True,
        ),
        _amount("vat_amount_fc", default_zero=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("vat_account_id", sa.Integer(), nullable=True),
        sa.Column("cogs_account_id", sa.Integer(), nullable=True),
        sa.Column("inventory_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "unit_cost_fc",
            sa.Numeric(precision=_UNIT_PRICE_PRECISION, scale=_UNIT_PRICE_SCALE),
            nullable=True,
        ),
        sa.Column("price_list_id", sa.Integer(), nullable=True),
        sa.Column("price_source", sa.String(length=_PRICE_SOURCE_MAX_LENGTH), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("extended_dimensions", JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("amount_fc > 0", name=op.f(f"ck_{table}_amount_positive")),
        sa.CheckConstraint("vat_amount_fc >= 0", name=op.f(f"ck_{table}_vat_not_negative")),
        sa.CheckConstraint(
            "discount_amount_fc >= 0", name=op.f(f"ck_{table}_discount_not_negative")
        ),
        sa.CheckConstraint(
            "discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)",
            name=op.f(f"ck_{table}_discount_percent_in_range"),
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name=op.f(f"ck_{table}_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_cost_fc IS NULL OR unit_cost_fc >= 0",
            name=op.f(f"ck_{table}_unit_cost_not_negative"),
        ),
        sa.CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL",
            name=op.f(f"ck_{table}_vat_account_required"),
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["sales_invoices.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        _account_fk(table, "account_id"),
        _account_fk(table, "vat_account_id"),
        _account_fk(table, "cogs_account_id"),
        _account_fk(table, "inventory_account_id"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_voucher", table, ["voucher_id", "line_no"])


def _create_sales_settlements() -> None:
    table = "sales_settlements"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        # `SettlementTargetKind`: 0 hóa đơn bán, 1 hóa đơn mua, 2 số dư đầu kỳ.
        sa.Column("target_kind", sa.SmallInteger(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        _amount("amount_fc"),
        _amount("amount"),
        _amount("fx_diff", default_zero=True),
        sa.CheckConstraint(
            "target_kind BETWEEN 0 AND 2", name=op.f(f"ck_{table}_target_kind_known")
        ),
        sa.CheckConstraint("amount_fc > 0", name=op.f(f"ck_{table}_amount_fc_positive")),
        sa.CheckConstraint("amount > 0", name=op.f(f"ck_{table}_amount_positive")),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["sales_invoices.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint(
            "voucher_id", "target_kind", "target_id", name=f"uq_{table}_voucher_target"
        ),
    )
    op.create_index(f"ix_{table}_target", table, ["target_kind", "target_id"])


def _apply_security() -> None:
    """Quyền cho vai trò runtime. Không RLS: ba bảng đều là con của `vouchers`
    (qua thân), phạm vi chi nhánh canh ở header."""
    grantee = _dataset_grantee()
    for table in _TABLES:
        for statement in grant_read_write(table, grantee=grantee, sequence=None):
            op.execute(statement)


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này đổi quyền xem báo
    cáo tuổi nợ phải thu sang module `sales`. Chỉ chạy online: bước đọc-rồi-ghi
    không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
