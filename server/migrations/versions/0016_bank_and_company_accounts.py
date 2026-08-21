"""Phân hệ Ngân hàng (SRS 04) + danh mục TK ngân hàng doanh nghiệp — lát 6A.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-21

Chạy **một lần cho mỗi schema dataset**, bằng `ket_owner`.

Năm bảng:

1. `company_bank_accounts` — danh mục thứ 21 (FR-BNK-002), khung `MasterDataRow`
   như 0003/0004; ở kernel vì sheet số dư đầu kỳ nhóm ngân hàng (hoãn từ 4C,
   làm ở 6D) cũng đọc nó mà `posting` không import được module `bank`.
2. `bank_vouchers` — thân chứng từ tiền gửi (báo có, UNC, séc, chuyển nội bộ),
   một-một với header `vouchers`.
3. `bank_voucher_lines` — dòng định khoản nháp, cùng hình dạng 0015.
4. `bank_statements` + 5. `bank_statement_lines` — sao kê đã nhập (RT-26) và
   trạng thái khớp đối chiếu (U5).

**Không** bật RLS cho bảng nào: danh mục theo lập luận 0003/0004 (dòng dùng
chung `branch_id IS NULL`); `bank_vouchers`/lines là bảng con của header đã
RLS; sao kê không mang `branch_id` — phạm vi của nó là phạm vi TK ngân hàng
chủ, và mọi màn hình đọc sao kê đều đi qua danh mục đó + RBAC. Miễn trừ khai
trong `tests/test_rls_policy_coverage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_CODE = 50
_NAME = 255
_PATH = 255
_DESCRIPTION = 500
_OPERATION_CODE = 50
_BANK_ACCOUNT = 50
_BANK_BRANCH = 255
_REFERENCE_NO = 100
_CHEQUE_NO = 50
_CURRENCY = 3

_CATALOG = "company_bank_accounts"


def upgrade() -> None:
    _create_company_bank_accounts()
    _create_bank_vouchers()
    _create_bank_voucher_lines()
    _create_bank_statements()
    _create_bank_statement_lines()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_company_bank_accounts() -> None:
    """Khung cột `MasterDataRow` lặp lại như 0004 (`_create_master_data_table`)
    thay vì import — migration là ảnh chụp lịch sử."""
    op.create_table(
        _CATALOG,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=_CODE), nullable=False),
        sa.Column("name", sa.String(length=_NAME), nullable=False),
        sa.Column("name_en", sa.String(length=_NAME), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.String(length=_PATH), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default=str(ROOT_LEVEL)),
        sa.Column("is_group", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bank_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=_CURRENCY), nullable=True),
        sa.Column("account_holder", sa.String(length=_NAME), nullable=True),
        sa.Column("bank_branch", sa.String(length=_BANK_BRANCH), nullable=True),
        sa.CheckConstraint(
            f"path ~ '{PATH_PATTERN}'", name=op.f(f"ck_{_CATALOG}_path_is_dotted_ids")
        ),
        sa.CheckConstraint(
            f"level >= {ROOT_LEVEL}", name=op.f(f"ck_{_CATALOG}_level_at_least_root")
        ),
        sa.CheckConstraint("code <> ''", name=op.f(f"ck_{_CATALOG}_code_not_blank")),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{_CATALOG}_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            [f"{_CATALOG}.id"],
            name=op.f(f"fk_{_CATALOG}_parent_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bank_id"], ["banks.id"], name=op.f(f"fk_{_CATALOG}_bank_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currencies.code"],
            name=op.f(f"fk_{_CATALOG}_currency_code"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_CATALOG}")),
        sa.UniqueConstraint("uid", name=op.f(f"uq_{_CATALOG}_uid")),
    )
    op.create_index(
        f"uq_{_CATALOG}_shared_code",
        _CATALOG,
        ["code"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NULL"),
    )
    op.create_index(
        f"uq_{_CATALOG}_branch_code",
        _CATALOG,
        ["branch_id", "code"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NOT NULL"),
    )
    op.create_index(f"ix_{_CATALOG}_path", _CATALOG, ["path"])
    op.create_index(f"ix_{_CATALOG}_parent_id", _CATALOG, ["parent_id"])
    op.create_index(op.f(f"ix_{_CATALOG}_branch_id"), _CATALOG, ["branch_id"])
    op.create_index(f"ix_{_CATALOG}_bank_id", _CATALOG, ["bank_id"])


def _create_bank_vouchers() -> None:
    table = "bank_vouchers"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("operation_code", sa.String(length=_OPERATION_CODE), nullable=True),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("counter_bank_account_id", sa.Integer(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("partner_kind", sa.SmallInteger(), nullable=True),
        sa.Column("beneficiary_name", sa.String(length=_NAME), nullable=True),
        sa.Column("beneficiary_account_no", sa.String(length=_BANK_ACCOUNT), nullable=True),
        sa.Column("beneficiary_bank_name", sa.String(length=_NAME), nullable=True),
        sa.Column("cheque_no", sa.String(length=_CHEQUE_NO), nullable=True),
        sa.Column("cheque_date", sa.Date(), nullable=True),
        sa.Column("reference_no", sa.String(length=_REFERENCE_NO), nullable=True),
        sa.CheckConstraint("kind BETWEEN 0 AND 3", name=op.f(f"ck_{table}_kind_known")),
        sa.CheckConstraint(
            "operation_code <> ''", name=op.f(f"ck_{table}_operation_code_not_blank")
        ),
        sa.CheckConstraint(
            "(kind = 3) = (operation_code IS NULL)",
            name=op.f(f"ck_{table}_operation_iff_not_transfer"),
        ),
        sa.CheckConstraint(
            "(kind = 3) = (counter_bank_account_id IS NOT NULL)",
            name=op.f(f"ck_{table}_counter_account_iff_transfer"),
        ),
        sa.CheckConstraint(
            "counter_bank_account_id IS NULL OR counter_bank_account_id <> bank_account_id",
            name=op.f(f"ck_{table}_counter_account_differs"),
        ),
        sa.CheckConstraint(
            "kind = 2 OR cheque_no IS NULL", name=op.f(f"ck_{table}_cheque_fields_only_on_cheque")
        ),
        sa.CheckConstraint(
            "(cheque_no IS NULL) = (cheque_date IS NULL)",
            name=op.f(f"ck_{table}_cheque_pair_complete"),
        ),
        sa.CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)",
            name=op.f(f"ck_{table}_partner_pair_complete"),
        ),
        sa.CheckConstraint(
            "partner_kind IS NULL OR partner_kind BETWEEN 0 AND 2",
            name=op.f(f"ck_{table}_partner_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["id"], ["vouchers.id"], name=op.f(f"fk_{table}_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bank_account_id"],
            [f"{_CATALOG}.id"],
            name=op.f(f"fk_{table}_bank_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["counter_bank_account_id"],
            [f"{_CATALOG}.id"],
            name=op.f(f"fk_{table}_counter_bank_account_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_bank_account", table, ["bank_account_id"])
    op.create_index(f"ix_{table}_reference_no", table, ["reference_no"])


def _create_bank_voucher_lines() -> None:
    table = "bank_voucher_lines"
    op.create_table(
        table,
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
            ["bank_vouchers.id"],
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


def _create_bank_statements() -> None:
    table = "bank_statements"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column(
            "opening_balance",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=True,
        ),
        sa.Column(
            "closing_balance",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=True,
        ),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("imported_by", sa.Integer(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["bank_account_id"],
            [f"{_CATALOG}.id"],
            name=op.f(f"fk_{table}_bank_account_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["bank_statement_profiles.id"],
            name=op.f(f"fk_{table}_profile_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_account_date", table, ["bank_account_id", "statement_date"])


def _create_bank_statement_lines() -> None:
    table = "bank_statement_lines"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("statement_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("reference_no", sa.String(length=_REFERENCE_NO), nullable=True),
        sa.Column("description", sa.String(length=_DESCRIPTION), nullable=True),
        sa.Column(
            "debit",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "credit",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
        sa.Column("matched_voucher_id", sa.Uuid(), nullable=True),
        sa.Column("match_kind", sa.SmallInteger(), nullable=False, server_default="2"),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f(f"ck_{table}_amounts_not_negative")
        ),
        sa.CheckConstraint("match_kind BETWEEN 0 AND 2", name=op.f(f"ck_{table}_match_kind_known")),
        sa.CheckConstraint(
            "(match_kind = 2) = (matched_voucher_id IS NULL)",
            name=op.f(f"ck_{table}_match_pair_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["bank_statements.id"],
            name=op.f(f"fk_{table}_statement_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_voucher_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{table}_matched_voucher_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_statement", table, ["statement_id", "line_no"])
    op.create_index(f"ix_{table}_matched_voucher", table, ["matched_voucher_id"])


def _apply_security() -> None:
    grantee = _dataset_grantee()
    for table, sequence in (
        (_CATALOG, serial_sequence_name(_CATALOG)),
        ("bank_vouchers", None),
        ("bank_voucher_lines", None),
        ("bank_statements", None),
        ("bank_statement_lines", None),
    ):
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("bank_statement_lines")
    op.drop_table("bank_statements")
    op.drop_table("bank_voucher_lines")
    op.drop_table("bank_vouchers")
    op.drop_table(_CATALOG)
