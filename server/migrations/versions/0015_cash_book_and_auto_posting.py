"""Phân hệ Quỹ tiền mặt (SRS 03) + định khoản tự động (FR-SYS-025) — lát 6A.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-21

Chạy **một lần cho mỗi schema dataset** như `0001`..`0014`, bằng `ket_owner`.

Năm bảng:

1. `auto_posting_rules` — nghiệp vụ thu/chi và cặp Nợ/Có ngầm định, thuộc gói
   cấu hình (cùng nhóm `default_accounts` của 0009). Dữ liệu builtin do
   `ensure_builtin_packages` gieo; dataset cấp trước 0015 được lấp qua đường
   backfill của seed (xem `kernel/config/packages/seed.py`).
2. `cash_vouchers` — thân phiếu thu/chi, một-một với header `vouchers` (0009).
3. `cash_voucher_lines` — dòng định khoản nháp.
4. `cash_settlements` — đối trừ công nợ của phiếu (`docs/srs/03` §4).
5. `treasurer_cash_book` — sổ quỹ của thủ quỹ (SRS 17), sổ song song với sổ
   kế toán, do thủ quỹ ghi.

**RLS chi nhánh** chỉ bật cho `treasurer_cash_book` — bảng duy nhất ở đây mang
`branch_id`. `cash_vouchers`/lines/settlements là bảng con của header
`vouchers` (đã RLS ở 0009): không có `branch_id` thì không có policy nào viết
được, và mọi đường đọc đi qua header — cùng lập luận `gl_journal_lines`.
Miễn trừ khai trong `tests/test_rls_policy_coverage.py`.

`downgrade()` drop cả năm bảng — chưa có bản cài phát hành, dữ liệu builtin
gieo lại được.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_DESCRIPTION = 500
_OPERATION_CODE = 50
_NAME = 255


def upgrade() -> None:
    _create_auto_posting_rules()
    _create_cash_vouchers()
    _create_cash_voucher_lines()
    _create_cash_settlements()
    _create_treasurer_cash_book()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_auto_posting_rules() -> None:
    op.create_table(
        "auto_posting_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("operation_code", sa.String(length=_OPERATION_CODE), nullable=False),
        sa.Column("operation_name", sa.String(length=_NAME), nullable=False),
        sa.Column("debit_purpose", sa.String(length=50), nullable=True),
        sa.Column("credit_purpose", sa.String(length=50), nullable=True),
        sa.Column(
            "requires_partner", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "document_type <> ''", name=op.f("ck_auto_posting_rules_document_type_not_blank")
        ),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f("ck_auto_posting_rules_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "operation_name <> ''", name=op.f("ck_auto_posting_rules_operation_name_not_blank")
        ),
        sa.CheckConstraint(
            "partner_kind IS NULL OR partner_kind BETWEEN 0 AND 2",
            name=op.f("ck_auto_posting_rules_partner_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_auto_posting_rules_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auto_posting_rules")),
        sa.UniqueConstraint(
            "package_id",
            "document_type",
            "operation_code",
            name="uq_auto_posting_rules_package_operation",
        ),
    )


def _create_cash_vouchers() -> None:
    op.create_table(
        "cash_vouchers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("operation_code", sa.String(length=_OPERATION_CODE), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("payer_receiver_name", sa.String(length=_NAME), nullable=True),
        sa.Column("attachment_count", sa.SmallInteger(), nullable=True),
        sa.Column("treasurer_status", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("treasurer_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("treasurer_posted_by", sa.Integer(), nullable=True),
        sa.Column("treasurer_book_date", sa.Date(), nullable=True),
        sa.CheckConstraint("kind BETWEEN 0 AND 1", name=op.f("ck_cash_vouchers_kind_known")),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f("ck_cash_vouchers_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "treasurer_status BETWEEN 0 AND 2",
            name=op.f("ck_cash_vouchers_treasurer_status_known"),
        ),
        sa.CheckConstraint(
            "(treasurer_posted_at IS NULL) = (treasurer_posted_by IS NULL)",
            name=op.f("ck_cash_vouchers_treasurer_stamp_complete"),
        ),
        sa.CheckConstraint(
            "(treasurer_status = 1) = (treasurer_book_date IS NOT NULL)",
            name=op.f("ck_cash_vouchers_treasurer_booked_has_date"),
        ),
        sa.CheckConstraint(
            "treasurer_status <> 1 OR treasurer_posted_at IS NOT NULL",
            name=op.f("ck_cash_vouchers_treasurer_booked_has_stamp"),
        ),
        sa.CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)",
            name=op.f("ck_cash_vouchers_partner_pair_complete"),
        ),
        sa.CheckConstraint(
            "partner_kind IS NULL OR partner_kind BETWEEN 0 AND 2",
            name=op.f("ck_cash_vouchers_partner_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["id"], ["vouchers.id"], name=op.f("fk_cash_vouchers_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["chart_of_accounts.id"],
            name=op.f("fk_cash_vouchers_cash_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_vouchers")),
    )
    op.create_index("ix_cash_vouchers_treasurer_status", "cash_vouchers", ["treasurer_status"])
    op.create_index("ix_cash_vouchers_cash_account", "cash_vouchers", ["cash_account_id"])


def _voucher_line_columns() -> list[sa.Column[object]]:
    """Bộ cột dòng định khoản dùng cho cả quỹ (0015) và ngân hàng (0016) —
    lặp lại ở 0016 chứ không import chéo, cùng lý do `_create_master_data_table`
    của 0004: migration là ảnh chụp lịch sử."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=_DESCRIPTION), nullable=True),
        sa.Column("debit_account_id", sa.Integer(), nullable=True),
        sa.Column("credit_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "amount_fc",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("cost_object_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("expense_item_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("extended_dimensions", JSONB(astext_type=sa.Text()), nullable=True),
    ]


def _create_cash_voucher_lines() -> None:
    table = "cash_voucher_lines"
    op.create_table(
        table,
        *_voucher_line_columns(),
        sa.CheckConstraint("amount_fc >= 0", name=op.f(f"ck_{table}_amount_not_negative")),
        sa.CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)",
            name=op.f(f"ck_{table}_partner_pair_complete"),
        ),
        sa.CheckConstraint(
            "partner_kind IS NULL OR partner_kind BETWEEN 0 AND 2",
            name=op.f(f"ck_{table}_partner_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["cash_vouchers.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["debit_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_debit_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["credit_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_credit_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_voucher", table, ["voucher_id", "line_no"])


def _create_cash_settlements() -> None:
    table = "cash_settlements"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.SmallInteger(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "amount_fc",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column(
            "amount", sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE), nullable=False
        ),
        sa.Column(
            "fx_diff",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "target_kind BETWEEN 0 AND 2", name=op.f(f"ck_{table}_target_kind_known")
        ),
        sa.CheckConstraint("amount_fc > 0", name=op.f(f"ck_{table}_amount_fc_positive")),
        sa.CheckConstraint("amount > 0", name=op.f(f"ck_{table}_amount_positive")),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["cash_vouchers.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint(
            "voucher_id", "target_kind", "target_id", name=f"uq_{table}_voucher_target"
        ),
    )
    op.create_index(f"ix_{table}_target", table, ["target_kind", "target_id"])


def _create_treasurer_cash_book() -> None:
    table = "treasurer_cash_book"
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("book_date", sa.Date(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column(
            "receipt_amount",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "payment_amount",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
        sa.Column("posted_by", sa.Integer(), nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "receipt_amount >= 0 AND payment_amount >= 0",
            name=op.f(f"ck_{table}_amounts_not_negative"),
        ),
        sa.CheckConstraint(
            "(receipt_amount > 0) <> (payment_amount > 0)",
            name=op.f(f"ck_{table}_exactly_one_side"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f(f"fk_{table}_branch_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["chart_of_accounts.id"],
            name=op.f(f"fk_{table}_cash_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("voucher_id", name=f"uq_{table}_voucher"),
    )
    op.create_index(f"ix_{table}_account_date", table, ["cash_account_id", "book_date"])


def _apply_security() -> None:
    """Quyền cho vai trò runtime + RLS chi nhánh cho sổ quỹ thủ quỹ.

    Hai bảng `SERIAL`/`BIGSERIAL` (`auto_posting_rules`, `treasurer_cash_book`)
    cần thêm quyền trên sequence — thiếu nó thì `INSERT` đầu tiên đổ với
    `permission denied for sequence` ở thao tác đầu tiên của người dùng.
    """
    grantee = _dataset_grantee()
    for table, sequence in (
        ("auto_posting_rules", serial_sequence_name("auto_posting_rules")),
        ("cash_vouchers", None),
        ("cash_voucher_lines", None),
        ("cash_settlements", None),
        ("treasurer_cash_book", serial_sequence_name("treasurer_cash_book")),
    ):
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)
    for statement in enable_branch_rls_statements("treasurer_cash_book", allow_null_branch=False):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("treasurer_cash_book")
    op.drop_table("cash_settlements")
    op.drop_table("cash_voucher_lines")
    op.drop_table("cash_vouchers")
    op.drop_table("auto_posting_rules")
