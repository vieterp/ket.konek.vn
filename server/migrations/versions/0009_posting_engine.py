"""Posting engine: header chứng từ, bảng phát sinh hai sổ, số dư, số dư ban đầu.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0008`, bằng `ket_owner`.

Mười bảng, theo bốn quyết định không đảo được của phase 4:

1. `config_packages` + `chart_of_accounts` — hình dạng lấy nguyên văn từ
   phase-05 (gói cấu hình TT200/TT133), tạo sớm ở đây vì `gl_postings.account_id`
   cần khóa ngoại ngay từ migration đầu — thêm FK sau lên bảng phát sinh triệu
   dòng là khóa bảng giữa giờ làm việc. Phase 5 đổ dữ liệu và dựng máy móc
   quanh hai bảng này, không đổi cột.
2. `vouchers` + `gl_journal_lines` — header chung mọi loại chứng từ (ADR-005)
   và bảng chi tiết đầu tiên thuộc module (`general_ledger.journal`).
3. `gl_postings` + `posting_dimension_values` — một bảng phát sinh cho mọi phân
   hệ, hai sổ bằng cột `ledger` (ADR-006), chiều mở rộng EAV (ADR-007). Kèm
   trigger `gl_postings_period_guard`: hàng rào cuối ở DB chặn INSERT/DELETE
   khi kỳ đã khóa — backstop cho đường ghi ngoài `PostingService`, không thay
   cho `FOR SHARE` ở tầng dịch vụ (RT-09).
4. `account_balances` + `balance_recalc_queue` (ADR-011, khóa gọn RT-22) và
   `opening_balances` + `opening_balance_invoices` (đủ cột cho 10 nhóm, logic
   nhóm 0–4 ở slice 4C, nhóm 5–9 ở phase 8 — RT-24).

**RLS chi nhánh** bật cho năm bảng mang `branch_id` (vouchers, gl_postings,
account_balances, balance_recalc_queue, opening_balances) — dữ liệu phát sinh
là chính thứ FR-SYS-072 sinh ra để cô lập. Bảng con không có `branch_id`
(`gl_journal_lines`, `posting_dimension_values`, `opening_balance_invoices`)
đi theo phạm vi của dòng cha qua join; hai bảng cấu hình
(`config_packages`, `chart_of_accounts`) dùng chung toàn dataset.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = (
    "vouchers",
    "gl_postings",
    "account_balances",
    "balance_recalc_queue",
    "opening_balances",
)

_GRANTS: tuple[tuple[str, str | None], ...] = (
    # (bảng, sequence của cột SERIAL/BIGSERIAL nếu có)
    ("config_packages", serial_sequence_name("config_packages")),
    ("chart_of_accounts", serial_sequence_name("chart_of_accounts")),
    ("vouchers", None),
    ("gl_journal_lines", None),
    ("gl_postings", serial_sequence_name("gl_postings")),
    ("posting_dimension_values", None),
    ("account_balances", serial_sequence_name("account_balances")),
    ("balance_recalc_queue", None),
    ("opening_balances", serial_sequence_name("opening_balances")),
    ("opening_balance_invoices", None),
)


def upgrade() -> None:
    _create_config_tables()
    _create_vouchers()
    _create_journal_lines()
    _create_gl_postings()
    _create_balances()
    _create_opening_balances()
    _create_period_guard()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_config_tables() -> None:
    op.create_table(
        "config_packages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("scheme", sa.String(length=10), nullable=False),
        sa.Column("legal_reference", sa.String(length=255), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "scheme IN ('TT200', 'TT133')", name=op.f("ck_config_packages_scheme_known")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_config_packages_effective_range_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_packages")),
        sa.UniqueConstraint("code", name=op.f("uq_config_packages_code")),
    )
    op.create_index(
        "ix_config_packages_scheme_effective",
        "config_packages",
        ["scheme", "effective_from"],
        unique=False,
    )
    op.create_table(
        "chart_of_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("level", sa.Integer(), server_default="1", nullable=False),
        sa.Column("balance_nature", sa.SmallInteger(), nullable=False),
        sa.Column("is_summary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "is_foreign_currency", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("detail_tracking", postgresql.ARRAY(sa.String(length=20)), nullable=True),
        sa.Column("is_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_inactive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("code <> ''", name=op.f("ck_chart_of_accounts_code_not_blank")),
        sa.CheckConstraint(
            "path ~ '^([0-9]+\\.)+$'", name=op.f("ck_chart_of_accounts_path_is_dotted_ids")
        ),
        sa.CheckConstraint(
            "balance_nature BETWEEN 0 AND 3", name=op.f("ck_chart_of_accounts_balance_nature_known")
        ),
        sa.CheckConstraint("level >= 1", name=op.f("ck_chart_of_accounts_level_at_least_root")),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_chart_of_accounts_package_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_chart_of_accounts_parent_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chart_of_accounts")),
        sa.UniqueConstraint("package_id", "code", name="uq_chart_of_accounts_package_code"),
    )
    op.create_index(
        "ix_chart_of_accounts_parent_id", "chart_of_accounts", ["parent_id"], unique=False
    )
    op.create_index(
        "ix_chart_of_accounts_path", "chart_of_accounts", ["package_id", "path"], unique=False
    )


def _create_vouchers() -> None:
    op.create_table(
        "vouchers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("voucher_no", sa.String(length=50), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("cashflow_activity", sa.SmallInteger(), nullable=True),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("reversal_of_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "(posted_at IS NULL) = (posted_by IS NULL)",
            name=op.f("ck_vouchers_posted_stamp_complete"),
        ),
        sa.CheckConstraint("exchange_rate > 0", name=op.f("ck_vouchers_exchange_rate_positive")),
        sa.CheckConstraint("status BETWEEN 1 AND 4", name=op.f("ck_vouchers_status_known")),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f("fk_vouchers_branch_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currencies.code"],
            name=op.f("fk_vouchers_currency_code"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["accounting_periods.id"],
            name=op.f("fk_vouchers_period_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of_id"],
            ["vouchers.id"],
            name=op.f("fk_vouchers_reversal_of_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vouchers")),
        sa.UniqueConstraint(
            "document_type", "branch_id", "voucher_no", name="uq_vouchers_type_branch_no"
        ),
    )
    op.create_index(
        "ix_vouchers_period", "vouchers", ["period_id", "document_type", "status"], unique=False
    )
    op.create_index("ix_vouchers_posting_date", "vouchers", ["posting_date"], unique=False)


def _create_journal_lines() -> None:
    op.create_table(
        "gl_journal_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("corresponding_account_id", sa.Integer(), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("debit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("extended_dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "NOT (debit_fc > 0 AND credit_fc > 0)", name=op.f("ck_gl_journal_lines_not_both_sides")
        ),
        sa.CheckConstraint(
            "debit_fc >= 0 AND credit_fc >= 0",
            name=op.f("ck_gl_journal_lines_amounts_not_negative"),
        ),
        sa.CheckConstraint(
            "exchange_rate > 0", name=op.f("ck_gl_journal_lines_exchange_rate_positive")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_gl_journal_lines_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f("fk_gl_journal_lines_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gl_journal_lines")),
    )
    op.create_index(
        "ix_gl_journal_lines_voucher", "gl_journal_lines", ["voucher_id", "line_no"], unique=False
    )


def _create_gl_postings() -> None:
    op.create_table(
        "gl_postings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("source_line_id", sa.Uuid(), nullable=True),
        sa.Column("ledger", sa.SmallInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("corresponding_account_id", sa.Integer(), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("debit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "NOT (debit > 0 AND credit > 0)", name=op.f("ck_gl_postings_not_both_sides")
        ),
        sa.CheckConstraint(
            "NOT (debit_fc > 0 AND credit_fc > 0)", name=op.f("ck_gl_postings_fc_not_both_sides")
        ),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f("ck_gl_postings_amounts_not_negative")
        ),
        sa.CheckConstraint(
            "debit_fc >= 0 AND credit_fc >= 0", name=op.f("ck_gl_postings_fc_amounts_not_negative")
        ),
        sa.CheckConstraint("ledger IN (0, 1)", name=op.f("ck_gl_postings_ledger_known")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_gl_postings_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f("fk_gl_postings_voucher_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gl_postings")),
    )
    op.create_index(
        "ix_gl_postings_acc_period",
        "gl_postings",
        ["ledger", "account_id", "period_id", "branch_id"],
        unique=False,
    )
    op.create_index("ix_gl_postings_date", "gl_postings", ["ledger", "posting_date"], unique=False)
    op.create_index(
        "ix_gl_postings_partner",
        "gl_postings",
        ["ledger", "partner_kind", "partner_id", "period_id"],
        unique=False,
        postgresql_where=sa.text("partner_id IS NOT NULL"),
    )
    op.create_index(
        "uq_gl_postings_line", "gl_postings", ["voucher_id", "ledger", "line_no"], unique=True
    )
    op.create_table(
        "posting_dimension_values",
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("dimension_id", sa.Integer(), nullable=False),
        sa.Column("value_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dimension_id"],
            ["analysis_dimensions.id"],
            name=op.f("fk_posting_dimension_values_dimension_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["gl_postings.id"],
            name=op.f("fk_posting_dimension_values_posting_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "posting_id", "dimension_id", name=op.f("pk_posting_dimension_values")
        ),
    )


def _create_balances() -> None:
    op.create_table(
        "account_balances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("ledger", sa.SmallInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("opening_debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("opening_credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("period_debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("period_credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("closing_debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("closing_credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("opening_debit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("period_debit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("ledger IN (0, 1)", name=op.f("ck_account_balances_ledger_known")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_account_balances_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["accounting_periods.id"],
            name=op.f("fk_account_balances_period_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_balances")),
    )
    op.create_index(
        "uq_account_balances_key",
        "account_balances",
        ["period_id", "ledger", "branch_id", "account_id", "currency_code"],
        unique=True,
    )
    op.create_table(
        "balance_recalc_queue",
        sa.Column("ledger", sa.SmallInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("from_period_id", sa.Integer(), nullable=False),
        sa.Column(
            "marked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.CheckConstraint("ledger IN (0, 1)", name=op.f("ck_balance_recalc_queue_ledger_known")),
        sa.ForeignKeyConstraint(
            ["from_period_id"],
            ["accounting_periods.id"],
            name=op.f("fk_balance_recalc_queue_from_period_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "ledger", "branch_id", "from_period_id", name=op.f("pk_balance_recalc_queue")
        ),
    )


def _create_opening_balances() -> None:
    op.create_table(
        "opening_balances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("fiscal_year_id", sa.Integer(), nullable=False),
        sa.Column("ledger", sa.SmallInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("debit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("detail_kind", sa.SmallInteger(), nullable=False),
        sa.Column("detail_ref_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0 AND debit_fc >= 0 AND credit_fc >= 0",
            name=op.f("ck_opening_balances_amounts_not_negative"),
        ),
        sa.CheckConstraint(
            "detail_kind BETWEEN 0 AND 9", name=op.f("ck_opening_balances_detail_kind_known")
        ),
        sa.CheckConstraint(
            "exchange_rate > 0", name=op.f("ck_opening_balances_exchange_rate_positive")
        ),
        sa.CheckConstraint("ledger IN (0, 1)", name=op.f("ck_opening_balances_ledger_known")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_opening_balances_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fiscal_year_id"],
            ["fiscal_years.id"],
            name=op.f("fk_opening_balances_fiscal_year_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opening_balances")),
    )
    op.create_index(
        "ix_opening_balances_key",
        "opening_balances",
        ["fiscal_year_id", "ledger", "branch_id", "account_id"],
        unique=False,
    )
    op.create_table(
        "opening_balance_invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opening_balance_id", sa.BigInteger(), nullable=False),
        sa.Column("invoice_no", sa.String(length=50), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("amount_fc", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("paid_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint(
            "amount >= 0 AND amount_fc >= 0",
            name=op.f("ck_opening_balance_invoices_amounts_not_negative"),
        ),
        sa.CheckConstraint(
            "paid_amount <= amount", name=op.f("ck_opening_balance_invoices_paid_within_amount")
        ),
        sa.CheckConstraint(
            "paid_amount >= 0", name=op.f("ck_opening_balance_invoices_paid_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["opening_balance_id"],
            ["opening_balances.id"],
            name=op.f("fk_opening_balance_invoices_opening_balance_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opening_balance_invoices")),
    )
    op.create_index(
        "ix_opening_balance_invoices_parent",
        "opening_balance_invoices",
        ["opening_balance_id"],
        unique=False,
    )


def _create_period_guard() -> None:
    """Hàng rào cuối ở DB: kỳ khóa thì `gl_postings` bất động (RT-09, backstop).

    Không thay cho `FOR SHARE` ở `PostingService` — trigger đọc trạng thái khóa
    ở snapshot của chính transaction ghi, nên cuộc đua ghi-sổ-vs-khóa-sổ vẫn do
    khóa dòng giải. Trigger chặn **đường ghi khác**: SQL tay, công cụ khôi
    phục, bug của một phase sau đi vòng qua service.
    """
    op.execute(
        """
        CREATE FUNCTION trg_block_locked_period() RETURNS trigger AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM accounting_periods p
                     WHERE p.id = COALESCE(NEW.period_id, OLD.period_id)
                       AND p.locked_at IS NOT NULL) THEN
            RAISE EXCEPTION 'PERIOD_LOCKED';
          END IF;
          RETURN COALESCE(NEW, OLD);
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER gl_postings_period_guard
          BEFORE INSERT OR DELETE ON gl_postings
          FOR EACH ROW EXECUTE FUNCTION trg_block_locked_period()
        """
    )


def _apply_security() -> None:
    """Quyền cho vai trò runtime + RLS chi nhánh (D3, RT-04).

    Bảng có cột `SERIAL`/`BIGSERIAL` phải cấp thêm quyền trên sequence — thiếu
    nó thì `INSERT` đầu tiên đổ với `permission denied for sequence` ở đúng
    thao tác đầu tiên của người dùng chứ không ở CI.
    """
    grantee = _dataset_grantee()
    for table, sequence in _GRANTS:
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)
    for table in _RLS_TABLES:
        for statement in enable_branch_rls_statements(table, allow_null_branch=False):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS gl_postings_period_guard ON gl_postings")
    op.execute("DROP FUNCTION IF EXISTS trg_block_locked_period()")
    op.drop_table("posting_dimension_values")
    op.drop_index("ix_opening_balance_invoices_parent", table_name="opening_balance_invoices")
    op.drop_table("opening_balance_invoices")
    op.drop_index("uq_gl_postings_line", table_name="gl_postings")
    op.drop_index(
        "ix_gl_postings_partner",
        table_name="gl_postings",
        postgresql_where=sa.text("partner_id IS NOT NULL"),
    )
    op.drop_index("ix_gl_postings_date", table_name="gl_postings")
    op.drop_index("ix_gl_postings_acc_period", table_name="gl_postings")
    op.drop_table("gl_postings")
    op.drop_index("ix_gl_journal_lines_voucher", table_name="gl_journal_lines")
    op.drop_table("gl_journal_lines")
    op.drop_index("ix_vouchers_posting_date", table_name="vouchers")
    op.drop_index("ix_vouchers_period", table_name="vouchers")
    op.drop_table("vouchers")
    op.drop_index("ix_opening_balances_key", table_name="opening_balances")
    op.drop_table("opening_balances")
    op.drop_table("balance_recalc_queue")
    op.drop_index("uq_account_balances_key", table_name="account_balances")
    op.drop_table("account_balances")
    op.drop_index("ix_chart_of_accounts_path", table_name="chart_of_accounts")
    op.drop_index("ix_chart_of_accounts_parent_id", table_name="chart_of_accounts")
    op.drop_table("chart_of_accounts")
    op.drop_index("ix_config_packages_scheme_effective", table_name="config_packages")
    op.drop_table("config_packages")
