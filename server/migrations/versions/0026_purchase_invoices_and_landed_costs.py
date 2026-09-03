"""Hóa đơn mua hàng + chi phí mua hàng — bốn bảng của module `purchase`.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-03

Chạy **một lần cho mỗi schema dataset** như `0001`..`0025`, bằng `ket_owner`.

Bốn bảng, cùng khuôn `cash_vouchers`/`cash_voucher_lines`/`cash_settlements`
(0015): thân một-một với header `vouchers`, dòng con `CASCADE` theo thân,
không bảng nào mang `branch_id` — phạm vi chi nhánh là của header và RLS canh
ở đó, nên ở đây chỉ cấp quyền, không bật RLS.

* `purchase_invoices` — thân hóa đơn mua (năm nghiệp vụ gom trong `kind`).
* `purchase_invoice_lines` — dòng hàng/dịch vụ: TK bên Nợ, thuế GTGT, phần chi
  phí mua hàng đã phân bổ, đủ bộ chiều phân tích.
* `landed_costs` — dòng chi phí mua hàng: TK Có riêng, NCC riêng, thuế riêng.
* `purchase_settlements` — đối trừ của hóa đơn trả lại vào hóa đơn gốc.

Cộng **một hàm chạy ngoài RLS** cho guard ngưỡng nợ (FR-SYS-032):
`partner_open_debt(p_partner_kind, p_partner_id, p_as_of)` trả các khoản còn nợ
sổ tài chính của MỘT đối tác **trên mọi chi nhánh**: dòng `ar_ap_ledger` chưa
tất toán, CỘNG chứng từ công nợ đầu kỳ còn nợ của niên độ phủ `p_as_of` — nợ
mang sang từ hệ thống cũ là nợ thật, không đếm nó thì ngưỡng nợ của một khách
hàng vừa cấp dữ liệu luôn còn nguyên và nợ quá hạn từ đầu kỳ không bao giờ kêu. `ar_ap_ledger` mang RLS theo
`branch_id` (0025), mà `partners.credit_limit` là ngưỡng của đối tác trước
toàn công ty: guard đọc dưới RLS của người ghi sổ chỉ thấy nợ ở chi nhánh
mình, và một đối tác nợ 900 ở A + 900 ở B lọt qua ngưỡng 1000 ở cả hai — lần
thứ tư của mẫu "phép kiểm chạy dưới phạm vi nào" (6C H-1, 3B-2 B-8, 6G-2 H-3).
Cùng cách vá và cùng lập luận an toàn với `0024`: `SECURITY DEFINER`, đầu vào
là một đối tác, không nhận điều kiện lọc tự do, không ghi, `SET search_path
FROM CURRENT` sau khi revision **tự đặt** search_path. Hàm trả các cột guard
cần để tính tổng còn nợ và nợ quá hạn — số chứng từ, hai ngày, số còn nợ —
không trả toàn bộ dòng sổ phụ.

Cộng **một sửa dữ liệu gói builtin**: `1331` bỏ theo dõi `item`. Gói TT99/
TT133 gieo từ lát 5A cho `1331` theo dõi vật tư, mà thuế GTGT đầu vào không
phải một sự thật theo vật tư — nó theo hóa đơn (tờ khai thuế, việc riêng). Với
validator "TK bật theo dõi thì dòng phải điền chiều", 1331 theo dõi `item` là
mọi dòng thuế của dịch vụ mua và của chi phí mua hàng (không có vật tư nào để
điền) đều bị từ chối trên dữ liệu thật. Đường gieo mầm không sửa TK đã gieo
(chỉ lấp nghiệp vụ/purpose thiếu), nên dataset đã cấp phải sửa ở đây; chỉ
đụng dòng builtin còn nguyên `{item}` — người dùng đã tự sửa thì giữ.

**`_refresh_builtin_data` CHUYỂN TỪ 0025 SANG ĐÂY**, cùng luật đã ghi ở 0025:
bước làm mới metadata builtin phải đứng ở cuối chuỗi mỗi khi chuỗi thêm thứ
dữ liệu builtin đọc. Lát này đổi quyền xem của báo cáo tuổi nợ phải trả sang
module `purchase`; để bước làm mới ở 0025 thì dataset đã ở 0025 không bao giờ
thấy thay đổi ấy.
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
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0026"
down_revision: str | None = "0025"
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

_OPERATION_CODE_MAX_LENGTH = 50
_DESCRIPTION_MAX_LENGTH = 500
_VENDOR_INVOICE_FORM_MAX_LENGTH = 20
_VENDOR_INVOICE_SERIAL_MAX_LENGTH = 20
_VENDOR_INVOICE_NO_MAX_LENGTH = 50

_TABLES = ("purchase_invoices", "purchase_invoice_lines", "landed_costs", "purchase_settlements")


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


def upgrade() -> None:
    _create_purchase_invoices()
    _create_purchase_invoice_lines()
    _create_landed_costs()
    _create_purchase_settlements()
    _apply_security()
    _create_partner_open_debt_function()
    _untrack_item_on_input_vat_account()
    _refresh_builtin_data()


def _create_purchase_invoices() -> None:
    table = "purchase_invoices"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        # `PurchaseInvoiceKind`: 0 mua nhập kho, 1 mua dịch vụ, 2 mua TSCĐ,
        # 3 hàng mua đang đi đường, 4 trả lại hàng mua — số trần có chú thích,
        # không import enum (migration là ảnh chụp lịch sử, 0022 nêu cùng luật).
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("operation_code", sa.String(length=_OPERATION_CODE_MAX_LENGTH), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        # `VendorInvoiceStatus`: 0 đã nhận hóa đơn, 1 chưa nhận, 2 không có.
        sa.Column("vendor_invoice_status", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "vendor_invoice_form", sa.String(length=_VENDOR_INVOICE_FORM_MAX_LENGTH), nullable=True
        ),
        sa.Column(
            "vendor_invoice_serial",
            sa.String(length=_VENDOR_INVOICE_SERIAL_MAX_LENGTH),
            nullable=True,
        ),
        sa.Column(
            "vendor_invoice_no", sa.String(length=_VENDOR_INVOICE_NO_MAX_LENGTH), nullable=True
        ),
        sa.Column("vendor_invoice_date", sa.Date(), nullable=True),
        sa.Column("payment_term_id", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("payable_account_id", sa.Integer(), nullable=False),
        # `LandedCostAllocation`: 0 theo giá trị, 1 theo số lượng, 2 nhập tay.
        sa.Column("landed_cost_allocation", sa.SmallInteger(), nullable=False, server_default="0"),
        _amount("total_before_tax_fc"),
        _amount("total_vat_fc"),
        _amount("total_landed_cost_fc"),
        _amount("total_fc"),
        sa.CheckConstraint("kind BETWEEN 0 AND 4", name=op.f(f"ck_{table}_kind_known")),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f(f"ck_{table}_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "vendor_invoice_status BETWEEN 0 AND 2",
            name=op.f(f"ck_{table}_vendor_invoice_status_known"),
        ),
        sa.CheckConstraint(
            "landed_cost_allocation BETWEEN 0 AND 2",
            name=op.f(f"ck_{table}_landed_cost_allocation_known"),
        ),
        sa.CheckConstraint(
            "total_before_tax_fc >= 0 AND total_vat_fc >= 0 "
            "AND total_landed_cost_fc >= 0 AND total_fc >= 0",
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
        sa.ForeignKeyConstraint(
            ["payable_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_payable_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_vendor", table, ["vendor_id"])


def _create_purchase_invoice_lines() -> None:
    table = "purchase_invoice_lines"
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
        _amount("amount_fc"),
        sa.Column(
            "vat_rate",
            sa.Numeric(precision=_VAT_RATE_PRECISION, scale=_VAT_RATE_SCALE),
            nullable=True,
        ),
        _amount("vat_amount_fc", default_zero=True),
        _amount("landed_cost_fc", default_zero=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("vat_account_id", sa.Integer(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("extended_dimensions", JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("amount_fc > 0", name=op.f(f"ck_{table}_amount_positive")),
        sa.CheckConstraint("vat_amount_fc >= 0", name=op.f(f"ck_{table}_vat_not_negative")),
        sa.CheckConstraint(
            "landed_cost_fc >= 0", name=op.f(f"ck_{table}_landed_cost_not_negative")
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name=op.f(f"ck_{table}_quantity_positive")
        ),
        sa.CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL",
            name=op.f(f"ck_{table}_vat_account_required"),
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["purchase_invoices.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vat_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_vat_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_voucher", table, ["voucher_id", "line_no"])


def _create_landed_costs() -> None:
    table = "landed_costs"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=_DESCRIPTION_MAX_LENGTH), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=True),
        sa.Column("credit_account_id", sa.Integer(), nullable=False),
        _amount("amount_fc"),
        sa.Column(
            "vat_rate",
            sa.Numeric(precision=_VAT_RATE_PRECISION, scale=_VAT_RATE_SCALE),
            nullable=True,
        ),
        _amount("vat_amount_fc", default_zero=True),
        sa.Column("vat_account_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("amount_fc >= 0", name=op.f(f"ck_{table}_amount_not_negative")),
        sa.CheckConstraint("vat_amount_fc >= 0", name=op.f(f"ck_{table}_vat_not_negative")),
        sa.CheckConstraint(
            "amount_fc > 0 OR vat_amount_fc > 0", name=op.f(f"ck_{table}_has_amount")
        ),
        sa.CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL",
            name=op.f(f"ck_{table}_vat_account_required"),
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["purchase_invoices.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["credit_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_credit_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vat_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_vat_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_voucher", table, ["voucher_id", "line_no"])


def _create_purchase_settlements() -> None:
    table = "purchase_settlements"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
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
            ["purchase_invoices.id"],
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
    """Quyền cho vai trò runtime. Không RLS: bốn bảng đều là con của
    `vouchers` (qua thân), phạm vi chi nhánh canh ở header."""
    grantee = _dataset_grantee()
    for table in _TABLES:
        for statement in grant_read_write(table, grantee=grantee, sequence=None):
            op.execute(statement)


def _create_partner_open_debt_function() -> None:
    """Xem docstring đầu tệp; khuôn và lý do từng mệnh đề ở `0024`."""
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION partner_open_debt("
        "p_partner_kind smallint, p_partner_id integer, p_as_of date)"
        " RETURNS TABLE("
        "document_no text, document_date date, due_date date, remaining numeric)"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT document_no::text, document_date, due_date, amount - settled"
        "     FROM ar_ap_ledger"
        "     WHERE partner_kind = p_partner_kind"
        "       AND partner_id = p_partner_id"
        "       AND ledger = 0"
        "       AND is_closed = FALSE"
        # Nhánh dưới đã đếm chứng từ đầu kỳ; ngày lượt chuyển năm gộp chúng
        # thành dòng sổ phụ (`opening_invoice_id`, xem `arap_matches_control`)
        # thì không có vế này là đếm hai lần và chặn ở nửa hạn mức thật.
        "       AND opening_invoice_id IS NULL"
        "     UNION ALL"
        "     SELECT COALESCE(i.invoice_no, '')::text,"
        "            COALESCE(i.invoice_date, y.start_date),"
        "            i.due_date,"
        "            i.amount - i.paid_amount"
        "     FROM opening_balance_invoices i"
        "     JOIN opening_balances o ON o.id = i.opening_balance_id"
        "     JOIN fiscal_years y ON y.id = o.fiscal_year_id"
        "     WHERE o.partner_kind = p_partner_kind"
        "       AND o.partner_id = p_partner_id"
        "       AND o.ledger = 0"
        # Nêu ĐỦ hai nhánh: loại đối tác khác (nhân viên) rơi vào NULL → không
        # dòng nào, thay vì lặng lẽ nhận nhóm "phải trả".
        "       AND o.detail_kind = CASE p_partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 END"
        "       AND i.amount_fc > i.paid_amount_fc"
        "       AND p_as_of BETWEEN y.start_date AND y.end_date"
        " $$"
    )
    # Guard hỏi theo ĐỐI TÁC trên mọi chi nhánh, còn `ix_arap_open` (0025) dẫn
    # đầu bằng `branch_id` — không có tiền tố khớp thì mỗi lượt ghi sổ tăng nợ
    # quét toàn bộ phần còn nợ của sổ phụ. Chỉ số bán phần thứ hai, cùng vị từ.
    op.create_index(
        "ix_arap_open_partner",
        "ar_ap_ledger",
        ["partner_kind", "partner_id"],
        postgresql_where=sa.text("is_closed = FALSE"),
    )
    # Nhánh thứ hai của hàm hỏi `opening_balances` theo đúng cặp ấy, mà chỉ số
    # sẵn có dẫn đầu bằng `fiscal_year_id` — không có chỉ số này thì mỗi lượt
    # ghi sổ tăng nợ quét cả hai bảng số dư đầu kỳ.
    op.create_index(
        "ix_opening_balances_partner",
        "opening_balances",
        ["partner_kind", "partner_id"],
        postgresql_where=sa.text("partner_id IS NOT NULL"),
    )


def _untrack_item_on_input_vat_account() -> None:
    """Xem docstring đầu tệp. SQL tĩnh nên chạy được cả `upgrade --sql`; không
    có bước ngược — trả `item` về cho 1331 là trả lại lỗi."""
    op.execute(
        sa.text(
            "UPDATE chart_of_accounts c"
            "   SET detail_tracking = NULL"
            "  FROM config_packages p"
            " WHERE p.id = c.package_id"
            "   AND p.is_builtin"
            "   AND c.code = '1331'"
            "   AND c.detail_tracking = ARRAY['item']::varchar[]"
        )
    )


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — **CHUYỂN TỪ 0025** (xem
    docstring đầu tệp). Chỉ chạy online: bước đọc-rồi-ghi không diễn đạt được
    thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS partner_open_debt(smallint, integer, date)")
    op.drop_index("ix_arap_open_partner", table_name="ar_ap_ledger")
    op.drop_index("ix_opening_balances_partner", table_name="opening_balances")
    for table in reversed(_TABLES):
        op.drop_table(table)
