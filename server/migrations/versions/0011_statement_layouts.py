"""Layout BCTC theo công thức: `statement_layouts` + `statement_rows` (FR-GLE-043).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0010`, bằng `ket_owner`.

Hai bảng đều là **cấu hình dùng chung toàn dataset** thuộc gói cấu hình, cùng
nhóm với `chart_of_accounts`/`default_accounts` (0009/0010 §RLS chi nhánh):
không mang `branch_id`, không bật RLS — chỉ cấp quyền đọc/ghi cho vai trò
runtime. Dữ liệu builtin (TT99 B01-DN/B02-DN, TT133 B01A-DNN/B02-DNN) do
`ensure_builtin_packages` gieo — với dataset cấp trước 0011, lượt gieo kế tiếp
lấp layout còn thiếu (`seed._ensure_statements_backfilled`).

`downgrade()` chỉ drop hai bảng: layout builtin gieo lại được nguyên vẹn;
layout đến từ gói `.zip` nhập ngoài mất theo — chấp nhận được vì gói nguồn
(`.zip` đã ký) vẫn ở ngoài hệ thống và nhập lại được.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENT_KINDS = "'balance_sheet', 'cashflow', 'income', 'notes'"

_GRANTS: tuple[tuple[str, str | None], ...] = (
    ("statement_layouts", serial_sequence_name("statement_layouts")),
    ("statement_rows", serial_sequence_name("statement_rows")),
)


def upgrade() -> None:
    _create_statement_layouts()
    _create_statement_rows()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_statement_layouts() -> None:
    op.create_table(
        "statement_layouts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=True),
        sa.Column("statement_kind", sa.String(length=20), nullable=False),
        sa.CheckConstraint("code <> ''", name=op.f("ck_statement_layouts_code_not_blank")),
        sa.CheckConstraint(
            f"statement_kind IN ({_STATEMENT_KINDS})",
            name=op.f("ck_statement_layouts_statement_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_statement_layouts_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_layouts")),
        sa.UniqueConstraint("package_id", "code", name="uq_statement_layouts_package_code"),
    )


def _create_statement_rows() -> None:
    op.create_table(
        "statement_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("layout_id", sa.Integer(), nullable=False),
        sa.Column("row_code", sa.String(length=10), nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column("label_en", sa.String(length=500), nullable=True),
        sa.Column("note_ref", sa.String(length=20), nullable=True),
        sa.Column("formula", sa.String(length=1000), nullable=False),
        sa.Column("indent_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_bold", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("hide_when_zero", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("row_code <> ''", name=op.f("ck_statement_rows_row_code_not_blank")),
        sa.CheckConstraint("formula <> ''", name=op.f("ck_statement_rows_formula_not_blank")),
        sa.ForeignKeyConstraint(
            ["layout_id"],
            ["statement_layouts.id"],
            name=op.f("fk_statement_rows_layout_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_rows")),
        sa.UniqueConstraint("layout_id", "row_code", name="uq_statement_rows_layout_row_code"),
    )


def _apply_security() -> None:
    """Quyền cho vai trò runtime — không RLS (cấu hình dùng chung toàn dataset, xem `0009`)."""
    grantee = _dataset_grantee()
    for table, sequence in _GRANTS:
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("statement_rows")
    op.drop_table("statement_layouts")
