"""Đối trừ theo nguyên tệ + biên bản kiểm kê quỹ — lát 6B.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-21

Chạy **một lần cho mỗi schema dataset** như `0001`..`0016`, bằng `ket_owner`.

Hai việc:

1. `opening_balance_invoices.paid_amount_fc` — trục nguyên tệ của đối trừ công
   nợ (lát 6B): `remaining_fc = amount_fc - paid_amount_fc`. Cột `paid_amount`
   (VND theo tỷ giá ghi nhận) đã có từ 0011 nhưng không suy ngược ra nguyên tệ
   được sau làm tròn. Bảng có dữ liệu thật thì cột mới phải `server_default` —
   dòng cũ chưa từng được đối trừ (chưa có đường ghi nào trước lát này) nên 0
   là đúng tuyệt đối, không phải giá trị đoán.
2. `cash_count_sheets` + `cash_count_sheet_lines` — kiểm kê quỹ (FR-QUY-030/031).
   RLS chi nhánh bật cho bảng cha; bảng dòng mệnh giá là bảng con không
   `branch_id`, đi qua cha — cùng lập luận `cash_voucher_lines` ở 0015, miễn
   trừ khai trong `tests/test_rls_policy_coverage.py`.

Cuối cùng là bước dữ liệu `_refresh_builtin_data` CHUYỂN TỪ 0014 sang (bài học
hạ tầng lát 6B): `refresh_builtin_reports` đọc dữ liệu builtin đóng gói HIỆN
TẠI và probe SQL của từng dataset — dataset `cash_forecast` (mới ở lát này)
tham chiếu chính cột `paid_amount_fc` vừa thêm ở trên, nên bước này chỉ chạy
được ở migration MỚI NHẤT. Migration tương lai thêm cột mà dataset builtin
tham chiếu phải dời tiếp bước này về cuối chuỗi — vẫn hợp lệ chừng nào chưa có
bản phát hành (xem docstring `refresh_builtin_reports` về doctrine sau đó).

`downgrade()` drop hai bảng và cột — chưa có bản cài phát hành.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_NOTE = 500


def upgrade() -> None:
    _add_paid_amount_fc()
    _create_cash_count_sheets()
    _create_cash_count_sheet_lines()
    _apply_security()
    _refresh_builtin_data()


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


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — CHUYỂN TỪ 0014 (xem
    docstring đầu tệp). Chỉ chạy online, cùng lý do với bản gốc: bước dữ liệu
    đọc-rồi-ghi không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def _add_paid_amount_fc() -> None:
    op.add_column(
        "opening_balance_invoices",
        sa.Column(
            "paid_amount_fc",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        op.f("ck_opening_balance_invoices_paid_fc_not_negative"),
        "opening_balance_invoices",
        "paid_amount_fc >= 0",
    )
    op.create_check_constraint(
        op.f("ck_opening_balance_invoices_paid_fc_within_amount"),
        "opening_balance_invoices",
        "paid_amount_fc <= amount_fc",
    )


def _create_cash_count_sheets() -> None:
    table = "cash_count_sheets"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("count_date", sa.Date(), nullable=False),
        sa.Column(
            "book_balance",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column(
            "counted_total",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=_NOTE), nullable=True),
        sa.Column("adjustment_voucher_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "counted_total >= 0", name=op.f(f"ck_{table}_counted_total_not_negative")
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
            ["adjustment_voucher_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{table}_adjustment_voucher_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_account_date", table, ["cash_account_id", "count_date"])


def _create_cash_count_sheet_lines() -> None:
    table = "cash_count_sheet_lines"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sheet_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column(
            "denomination",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("denomination > 0", name=op.f(f"ck_{table}_denomination_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f(f"ck_{table}_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["sheet_id"],
            ["cash_count_sheets.id"],
            name=op.f(f"fk_{table}_sheet_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_sheet", table, ["sheet_id", "line_no"])


def _apply_security() -> None:
    grantee = _dataset_grantee()
    for table in ("cash_count_sheets", "cash_count_sheet_lines"):
        for statement in grant_read_write(table, grantee=grantee, sequence=None):
            op.execute(statement)
    for statement in enable_branch_rls_statements("cash_count_sheets", allow_null_branch=False):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("cash_count_sheet_lines")
    op.drop_table("cash_count_sheets")
    op.drop_constraint(
        "ck_opening_balance_invoices_paid_fc_within_amount", "opening_balance_invoices"
    )
    op.drop_constraint(
        "ck_opening_balance_invoices_paid_fc_not_negative", "opening_balance_invoices"
    )
    op.drop_column("opening_balance_invoices", "paid_amount_fc")
