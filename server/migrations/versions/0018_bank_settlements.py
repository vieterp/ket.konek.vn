"""Đối trừ công nợ của chứng từ tiền gửi — lát 6C.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-21

Chạy **một lần cho mỗi schema dataset** như `0001`..`0017`, bằng `ket_owner`.

Một việc: `bank_settlements` — bản sao hình dạng của `cash_settlements` (0015)
với FK trỏ `bank_vouchers` (FR-BNK-007: đối trừ công nợ khi thu/chi tiền gửi
giống cơ chế phân hệ Quỹ; cơ chế dùng chung ở `ket.posting.settlements`, mỗi
module một bảng để FK trỏ đúng bảng thân của mình).

**Không RLS**: bảng con của header `vouchers` (đã RLS ở 0009), không mang
`branch_id` — cùng lập luận `cash_settlements` (0015); miễn trừ khai trong
`tests/test_rls_policy_coverage.py`.

Bước `_refresh_builtin_data` GIỮ ở 0017: không dataset builtin nào đọc
`bank_settlements`, nên 0017 chạy giữa chuỗi vẫn lành (doctrine 6B chỉ đòi dời
khi migration sau thêm cột mà dữ liệu builtin tham chiếu).

`downgrade()` drop bảng — chưa có bản cài phát hành.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2


def upgrade() -> None:
    _create_bank_settlements()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_bank_settlements() -> None:
    table = "bank_settlements"
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
            ["bank_vouchers.id"],
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
    grantee = _dataset_grantee()
    for statement in grant_read_write("bank_settlements", grantee=grantee, sequence=None):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("bank_settlements")
