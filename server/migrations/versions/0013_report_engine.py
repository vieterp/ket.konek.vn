"""Metadata report engine: 4 bảng `report_*` (FR-RPT-001..003).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-20

Chạy **một lần cho mỗi schema dataset** như `0001`..`0012`, bằng `ket_owner`.

Cả bốn bảng là **cấu hình dùng chung toàn dataset**, cùng nhóm
`chart_of_accounts`/`statement_layouts` (0009/0011): không `branch_id`, không
RLS — cô lập chi nhánh nằm ở RLS của các bảng gốc mà `sql_text` đọc, cộng lớp
bọc phạm vi của engine (phòng thủ lớp hai, BR-RPT-04/05). Chỉ cấp quyền
đọc/ghi cho vai trò runtime.

Dữ liệu builtin do `ensure_builtin_reports` gieo lúc cấp dữ liệu kế toán; với
dataset cấp trước 0013, lượt gieo kế tiếp lấp dòng còn thiếu (idempotent theo
từng dòng — xem docstring `kernel/config/reports/seed.py`).

`downgrade()` chỉ drop bốn bảng: dữ liệu builtin gieo lại được nguyên vẹn;
`report_saved_params` (FR-RPT-005, hoãn v1.1) chưa tồn tại nên không có dữ
liệu người dùng nào để mất.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LAYOUT_KINDS = "'form', 'grouped', 'statement', 'table'"
_LEDGER_SCOPES = "'both', 'financial', 'management'"

_GRANTS: tuple[tuple[str, str | None], ...] = (
    ("report_datasets", None),
    ("report_layouts", None),
    ("report_param_sets", None),
    ("report_definitions", serial_sequence_name("report_definitions")),
)


def upgrade() -> None:
    _create_report_datasets()
    _create_report_layouts()
    _create_report_param_sets()
    _create_report_definitions()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_report_datasets() -> None:
    op.create_table(
        "report_datasets",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("allowed_params", ARRAY(sa.Text()), nullable=False),
        sa.Column("supports_branch", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("supports_ledger", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        # Mặc định FALSE — không-tin-cậy trừ khi seed builtin nói khác (RT-07).
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.CheckConstraint("sql_text <> ''", name=op.f("ck_report_datasets_sql_text_not_blank")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_report_datasets")),
    )


def _create_report_layouts() -> None:
    op.create_table(
        "report_layouts",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(f"kind IN ({_LAYOUT_KINDS})", name=op.f("ck_report_layouts_kind_known")),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_report_layouts_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_report_layouts")),
    )


def _create_report_param_sets() -> None:
    op.create_table(
        "report_param_sets",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_report_param_sets")),
    )


def _create_report_definitions() -> None:
    op.create_table(
        "report_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("module", sa.String(length=50), nullable=False),
        sa.Column("dataset_code", sa.String(length=50), nullable=False),
        sa.Column("layout_code", sa.String(length=50), nullable=False),
        sa.Column("param_set_code", sa.String(length=50), nullable=False),
        sa.Column("ledger_scope", sa.String(length=20), server_default="both", nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "code ~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'",
            name=op.f("ck_report_definitions_code_url_safe"),
        ),
        sa.CheckConstraint(
            f"ledger_scope IN ({_LEDGER_SCOPES})",
            name=op.f("ck_report_definitions_ledger_scope_known"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_code"],
            ["report_datasets.code"],
            name=op.f("fk_report_definitions_dataset_code"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["layout_code"],
            ["report_layouts.code"],
            name=op.f("fk_report_definitions_layout_code"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["param_set_code"],
            ["report_param_sets.code"],
            name=op.f("fk_report_definitions_param_set_code"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_report_definitions_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_definitions")),
        sa.UniqueConstraint("code", name="uq_report_definitions_code"),
    )


def _apply_security() -> None:
    """Quyền cho vai trò runtime — không RLS (cấu hình dùng chung, xem docstring đầu tệp)."""
    grantee = _dataset_grantee()
    for table, sequence in _GRANTS:
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("report_definitions")
    op.drop_table("report_param_sets")
    op.drop_table("report_layouts")
    op.drop_table("report_datasets")
