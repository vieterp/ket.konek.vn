"""Sổ phụ công nợ `ar_ap_ledger` + chiều chi nhánh cho hóa đơn số dư — lát 7A.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-01

Chạy **một lần cho mỗi schema dataset** như `0001`..`0024`, bằng `ket_owner`.

Ba việc, hai trong số đó là trả nợ:

1. **`ar_ap_ledger`** — sổ phụ công nợ theo từng chứng từ, chủ sở hữu là module
   `receivables` (RT-18). Khóa chính là **UUID chứ không BIGSERIAL** như phác
   thảo `phase-07` §Architecture: dòng ở đây là đích đối trừ, và
   `SettlementTargetSource.find/apply` của kernel — đóng băng từ phase 6, ADR-020
   — cầm `target_id: UUID`. Khóa bigint thì `cash_settlements.target_id` không
   trỏ tới được, tức bảng mất đúng công dụng nó sinh ra để làm.

2. **`opening_balance_invoices.branch_id`** — trả nợ 4C. Bảng ấy từ `0009` cố ý
   không mang `branch_id` ("phạm vi là phạm vi dòng cha, qua join"). Lập luận
   đó đúng cho các đường ORM đang có, nhưng nó để lại một lỗ **có hình dạng**:
   cổng `test_rls_policy_coverage` chỉ soi bảng CÓ cột `branch_id`, nên bảng
   này không bao giờ bị hỏi tới, và cũng không có dòng `_EXEMPT` nào ghi lại
   quyết định. Một `SELECT` thẳng từ bảng — báo cáo mới, truy vấn tay, một lát
   tương lai quên join cha — đọc được mọi chi nhánh và **không có gì đỏ**. Từ
   lát 7A số lượng cửa đọc công nợ tăng hẳn, nên vá trước khi mở cửa.

3. **`_refresh_builtin_data` CHUYỂN TỪ 0023 SANG ĐÂY.** Bắt buộc, không phải
   dọn dẹp: lát này thêm dataset báo cáo `ar_ap_aging` đọc bảng `ar_ap_ledger`
   vừa tạo ở bước 1. `refresh_builtin_reports` **dò SQL của mọi dataset builtin
   bằng `LIMIT 0`** trước khi ghi, nên chạy nó ở 0023 — trước khi bảng tồn tại
   — thì phép dò gãy; và để nguyên nó ở 0023 thì mọi dataset đã ở revision 0023
   trở lên **không bao giờ** thấy báo cáo tuổi nợ. Đây là bẫy "dataset cũ không
   nhận backfill" mà dự án đã sập **hai lần** (6A, 6G-1); bước làm-mới phải ở
   cuối chuỗi mỗi khi chuỗi mọc thêm thứ dữ liệu builtin đọc.

Thứ tự trong `upgrade()` vì thế bị ép: tạo bảng và cột trước, làm mới sau.
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

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_RATE_PRECISION = 18
_RATE_SCALE = 6

_DOCUMENT_NO_MAX_LENGTH = 50
_DESCRIPTION_MAX_LENGTH = 500
_CURRENCY_CODE_LENGTH = 3


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


def upgrade() -> None:
    _create_ar_ap_ledger()
    _add_opening_invoice_branch()
    _apply_security()
    _refresh_builtin_data()


def _create_ar_ap_ledger() -> None:
    table = "ar_ap_ledger"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        # Giá trị của `kernel.protocols.SettlementTargetKind` — viết bằng số
        # trần có chú thích, không import enum: migration là ảnh chụp lịch sử,
        # còn enum trong mã nguồn đi tiếp (0022 nêu cùng luật).
        # 0 hóa đơn bán, 1 hóa đơn mua, 2 hóa đơn số dư đầu kỳ.
        sa.Column("target_kind", sa.SmallInteger(), nullable=False),
        # `kernel.contracts.PartnerKind`: 0 khách hàng, 1 NCC, 2 nhân viên.
        sa.Column("partner_kind", sa.SmallInteger(), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("ledger", sa.SmallInteger(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("opening_invoice_id", sa.Uuid(), nullable=True),
        sa.Column("document_no", sa.String(length=_DOCUMENT_NO_MAX_LENGTH), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency_code", sa.String(length=_CURRENCY_CODE_LENGTH), nullable=False),
        sa.Column(
            "exchange_rate",
            sa.Numeric(precision=_RATE_PRECISION, scale=_RATE_SCALE),
            nullable=False,
        ),
        sa.Column(
            "amount_fc",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column(
            "amount",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
        ),
        sa.Column(
            "settled_fc",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "settled",
            sa.Numeric(precision=_AMOUNT_PRECISION, scale=_AMOUNT_SCALE),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Cột SINH, không phải cờ ứng dụng ghi: một cờ do ứng dụng giữ sẽ lệch
        # với `settled` ở lần đầu có đường ghi quên cập nhật, và chỉ số bán
        # phần `ix_arap_open` — thứ mọi màn chọn đối trừ đi qua — dựa vào nó.
        sa.Column(
            "is_closed",
            sa.Boolean(),
            sa.Computed("settled >= amount", persisted=True),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=_DESCRIPTION_MAX_LENGTH), nullable=True),
        sa.CheckConstraint(
            "target_kind BETWEEN 0 AND 2", name=op.f(f"ck_{table}_target_kind_known")
        ),
        sa.CheckConstraint(
            "partner_kind BETWEEN 0 AND 2", name=op.f(f"ck_{table}_partner_kind_known")
        ),
        sa.CheckConstraint("ledger BETWEEN 0 AND 1", name=op.f(f"ck_{table}_ledger_known")),
        sa.CheckConstraint(
            "amount >= 0 AND amount_fc >= 0", name=op.f(f"ck_{table}_amounts_not_negative")
        ),
        sa.CheckConstraint("settled >= 0", name=op.f(f"ck_{table}_settled_not_negative")),
        sa.CheckConstraint("settled_fc >= 0", name=op.f(f"ck_{table}_settled_fc_not_negative")),
        # Chặn chót RT-16 chống đối trừ vượt: `settlement_source.apply` khóa
        # dòng và trả 422 trước; lọt qua nó thì DB nổ IntegrityError chứ không
        # lặng lẽ ghi một khoản nợ âm.
        sa.CheckConstraint("settled <= amount", name=op.f(f"ck_{table}_settled_within_amount")),
        sa.CheckConstraint(
            "settled_fc <= amount_fc", name=op.f(f"ck_{table}_settled_fc_within_amount")
        ),
        sa.CheckConstraint(
            "document_id IS NOT NULL OR opening_invoice_id IS NOT NULL",
            name=op.f(f"ck_{table}_has_a_source_document"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{table}_document_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opening_invoice_id"],
            ["opening_balance_invoices.id"],
            name=op.f(f"fk_{table}_opening_invoice_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    # Chỉ số BÁN PHẦN: màn chọn đối trừ và thẻ công nợ chỉ hỏi khoản CÒN nợ, và
    # sổ phụ nhiều năm thì phần đã tất toán chiếm gần hết bảng.
    # `branch_id` dẫn đầu — xem lập luận ở `modules/receivables/models.py`:
    # RLS, hai dataset báo cáo và check toàn vẹn đều lọc chi nhánh trước tiên.
    op.create_index(
        "ix_arap_open",
        table,
        ["branch_id", "partner_kind", "partner_id"],
        postgresql_where=sa.text("is_closed = FALSE"),
    )
    op.create_index(f"ix_{table}_document", table, ["document_id"])


def _add_opening_invoice_branch() -> None:
    """Trả nợ 4C: cột `branch_id` + policy RLS cho `opening_balance_invoices`.

    Backfill từ dòng cha, rồi mới `SET NOT NULL` — cột thêm vào bảng có dữ liệu
    phải qua ba bước ấy, không rút gọn được thành một.

    `allow_null_branch=False`: `opening_balances.branch_id` là NOT NULL, nên
    sau backfill không dòng nào có quyền để trống. Khác `bank_statements` (0022)
    nơi NULL nghĩa là "tài khoản dùng chung toàn công ty" — ở đây NULL không có
    nghĩa nghiệp vụ nào, và cho phép nó là mở lại đúng cái lỗ vừa vá.
    """
    op.add_column("opening_balance_invoices", sa.Column("branch_id", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE opening_balance_invoices t"
            "   SET branch_id = p.branch_id"
            "  FROM opening_balances p"
            " WHERE p.id = t.opening_balance_id"
            "   AND t.branch_id IS DISTINCT FROM p.branch_id"
        )
    )
    op.alter_column("opening_balance_invoices", "branch_id", nullable=False)
    op.create_index("ix_opening_balance_invoices_branch", "opening_balance_invoices", ["branch_id"])
    for statement in enable_branch_rls_statements("opening_balance_invoices"):
        op.execute(statement)


def _apply_security() -> None:
    grantee = _dataset_grantee()
    for statement in grant_read_write("ar_ap_ledger", grantee=grantee, sequence=None):
        op.execute(statement)
    for statement in enable_branch_rls_statements("ar_ap_ledger"):
        op.execute(statement)


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — **CHUYỂN TỪ 0023** (xem
    docstring đầu tệp: dataset `ar_ap_aging` đọc bảng vừa tạo ở trên, nên bước
    này phải ở cuối chuỗi). Chỉ chạy online: bước đọc-rồi-ghi không diễn đạt
    được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON opening_balance_invoices")
    op.execute("ALTER TABLE opening_balance_invoices DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_opening_balance_invoices_branch", table_name="opening_balance_invoices")
    op.drop_column("opening_balance_invoices", "branch_id")

    op.execute("DROP POLICY IF EXISTS p_branch_scope ON ar_ap_ledger")
    op.execute("ALTER TABLE ar_ap_ledger DISABLE ROW LEVEL SECURITY")
    op.drop_table("ar_ap_ledger")
