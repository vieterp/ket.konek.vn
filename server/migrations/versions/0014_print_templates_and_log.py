"""Mẫu in chứng từ + sổ theo dõi lần in (FR-RPT-008/011) và làm mới metadata
báo cáo builtin theo mã mẫu thông tư (lát 5D).

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-20

Chạy **một lần cho mỗi schema dataset** như `0001`..`0013`, bằng `ket_owner`.

* `print_templates` — cấu hình dùng chung toàn dataset, không RLS (cùng nhóm
  `report_layouts`); quyền đọc/ghi cho vai trò runtime.
* `print_log` — **chỉ-thêm** (`grants.APPEND_ONLY_TABLES`): vai trò runtime
  không sửa/xóa được lịch sử in; RLS theo chi nhánh.
* Dữ liệu: bước `refresh_builtin_reports` từng nằm ở đây (lát 5D) đã CHUYỂN
  sang migration mới nhất (0017, lát 6B): hàm đọc dữ liệu builtin ĐÓNG GÓI
  HIỆN TẠI và probe SQL của nó — chạy giữa chuỗi migration thì dataset nào
  tham chiếu cột ra đời sau 0014 (`cash_forecast` cần `paid_amount_fc` của
  0017) sẽ đổ ngay tại đây. Bước làm-mới-theo-dữ-liệu-hiện-tại chỉ đúng ở
  ĐẦU chuỗi (head) — bài học hạ tầng của phase 6, bước 22.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import (
    grant_append_only,
    grant_read_write,
    serial_sequence_name,
)
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    _create_print_templates()
    _create_print_log()
    _apply_security()


def _create_print_templates() -> None:
    op.create_table(
        "print_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("html_template", sa.Text(), nullable=False),
        sa.Column("css_extra", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # KHÔNG-tin-cậy là mặc định (cùng chiều `report_datasets`, review 5C M1).
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "code ~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'", name=op.f("ck_print_templates_code_url_safe")
        ),
        sa.CheckConstraint(
            "html_template <> ''", name=op.f("ck_print_templates_html_template_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_print_templates_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_print_templates")),
        sa.UniqueConstraint("document_type", "code", name="uq_print_templates_type_code"),
    )
    op.create_index(
        "uq_print_templates_default_per_type",
        "print_templates",
        ["document_type"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def _create_print_log() -> None:
    op.create_table(
        "print_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("template_code", sa.String(length=50), nullable=False),
        sa.Column("copy_no", sa.Integer(), nullable=False),
        sa.Column(
            "printed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("printed_by", sa.Integer(), nullable=False),
        sa.CheckConstraint("copy_no >= 1", name=op.f("ck_print_log_copy_no_positive")),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f("fk_print_log_voucher_id"),
            # CASCADE: xóa chứng từ nháp đã từng in là luồng hợp lệ — log mồ
            # côi sẽ chặn chính việc xóa nháp (xem `reporting/printing/models.py`).
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_print_log_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_print_log")),
    )
    op.create_index(
        "uq_print_log_voucher_copy", "print_log", ["voucher_id", "copy_no"], unique=True
    )
    op.create_index("ix_print_log_voucher", "print_log", ["voucher_id"])


def _apply_security() -> None:
    grantee = role_name_for_schema(_target_schema())
    for statement in grant_read_write(
        "print_templates", grantee=grantee, sequence=serial_sequence_name("print_templates")
    ):
        op.execute(statement)
    for statement in grant_append_only(
        "print_log", grantee=grantee, sequence=serial_sequence_name("print_log")
    ):
        op.execute(statement)
    for statement in enable_branch_rls_statements("print_log", allow_null_branch=False):
        op.execute(statement)


def downgrade() -> None:
    # Metadata báo cáo builtin không đảo về nội dung 0013 — cùng lập luận
    # `0013.downgrade`: dữ liệu builtin gieo lại được; mã `SO-CAI` cũ không
    # còn giá trị để khôi phục.
    op.drop_index("ix_print_log_voucher", table_name="print_log")
    op.drop_index("uq_print_log_voucher_copy", table_name="print_log")
    op.drop_table("print_log")
    op.drop_index("uq_print_templates_default_per_type", table_name="print_templates")
    op.drop_table("print_templates")
