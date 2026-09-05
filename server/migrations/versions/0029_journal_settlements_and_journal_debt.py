"""Đối trừ công nợ trên chứng từ nghiệp vụ khác + nới chiều sổ phụ.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-05

Chạy **một lần cho mỗi schema dataset** như `0001`..`0028`, bằng `ket_owner`.

Hai việc, cùng một quyết định (user 2026-09-05): bút toán gõ thẳng vào TK công
nợ vẫn cho phép, nhưng từ nay **sinh dòng sổ phụ tự động** —
`general_ledger.journal` trở thành nguồn ghi `ar_ap_ledger` thứ ba, bên cạnh
`purchase` và `sales`.

* `gl_journal_settlements` — dòng đối trừ của chứng từ nghiệp vụ khác. Khác ba
  bảng đối trừ trước ở đúng một cột: nó gắn vào **dòng định khoản**, không vào
  chứng từ. Một bút toán bù trừ 131 ↔ 331 chạm hai TK công nợ và một bút toán
  phân loại lại đầu năm chạm nhiều đối tác, nên "chứng từ này giảm nợ của hóa
  đơn nào" không phải câu trả lời được ở mức chứng từ.
* `ar_ap_ledger.target_kind` nới từ `0..2` lên `0..4` — hai giá trị mới
  `JOURNAL_RECEIVABLE`/`JOURNAL_PAYABLE`. Hai chứ không một cộng thêm cột
  chiều: bất biến từ 7A là mỗi `target_kind` mang đúng một chiều nợ, và chính
  nó cho hai view provider khóa được chiều mà không đọc thêm cột nào.

Không có bước dữ liệu: lát này không thêm nghiệp vụ định khoản, không đổi
metadata builtin nào, nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2

_SETTLEMENTS_TABLE = "gl_journal_settlements"
_LEDGER_TABLE = "ar_ap_ledger"
_LEDGER_TARGET_KIND_CHECK = "ck_ar_ap_ledger_target_kind_known"


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
    _widen_ledger_target_kind()
    _create_journal_settlements()
    _apply_security()


def _widen_ledger_target_kind() -> None:
    """Nới `target_kind` để nhận hai loại đích của chứng từ nghiệp vụ khác."""
    # Tên ĐẦY ĐỦ và KHÔNG truyền `type_`: có `type_` thì Alembic áp quy ước
    # đặt tên lên chuỗi đưa vào và dán thêm một tiền tố `ck_<bảng>_` nữa. Cùng
    # lối `0002` đã dùng.
    op.drop_constraint(_LEDGER_TARGET_KIND_CHECK, _LEDGER_TABLE)
    op.create_check_constraint("target_kind_known", _LEDGER_TABLE, "target_kind BETWEEN 0 AND 4")


def _create_journal_settlements() -> None:
    table = _SETTLEMENTS_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("journal_line_id", sa.Uuid(), nullable=False),
        # `SettlementTargetKind`: 0 hóa đơn bán, 1 hóa đơn mua, 2 số dư đầu kỳ,
        # 3 phải thu ghi tay, 4 phải trả ghi tay — số trần có chú thích, không
        # import enum (migration là ảnh chụp lịch sử, 0022 nêu cùng luật).
        sa.Column("target_kind", sa.SmallInteger(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        _amount("amount_fc"),
        _amount("amount"),
        _amount("fx_diff", default_zero=True),
        sa.CheckConstraint(
            "target_kind BETWEEN 0 AND 4", name=op.f(f"ck_{table}_target_kind_known")
        ),
        sa.CheckConstraint("amount_fc > 0", name=op.f(f"ck_{table}_amount_fc_positive")),
        sa.CheckConstraint("amount > 0", name=op.f(f"ck_{table}_amount_positive")),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["vouchers.id"],
            name=op.f(f"fk_{table}_voucher_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["journal_line_id"],
            ["gl_journal_lines.id"],
            name=op.f(f"fk_{table}_journal_line_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        # Duy nhất theo DÒNG, không theo chứng từ: hai dòng của cùng một bút
        # toán bù trừ được phép trỏ vào cùng một hóa đơn đích.
        sa.UniqueConstraint(
            "journal_line_id", "target_kind", "target_id", name=f"uq_{table}_line_target"
        ),
    )
    op.create_index(f"ix_{table}_voucher", table, ["voucher_id"])
    op.create_index(f"ix_{table}_target", table, ["target_kind", "target_id"])


def _apply_security() -> None:
    """Quyền cho vai trò runtime. Không RLS: bảng là con của `vouchers`, phạm
    vi chi nhánh canh ở header — cùng luật với ba bảng đối trừ trước."""
    grantee = _dataset_grantee()
    for statement in grant_read_write(_SETTLEMENTS_TABLE, grantee=grantee, sequence=None):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(_SETTLEMENTS_TABLE)
    op.drop_constraint(_LEDGER_TARGET_KIND_CHECK, _LEDGER_TABLE)
    op.create_check_constraint("target_kind_known", _LEDGER_TABLE, "target_kind BETWEEN 0 AND 2")
