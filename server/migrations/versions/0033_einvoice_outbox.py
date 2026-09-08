"""Hàng đợi truyền tải hóa đơn điện tử — lát 7E-1.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-08

Chạy **một lần cho mỗi schema dataset** như `0001`..`0032`, bằng `ket_owner`.

**Một bảng, `einvoice_outbox`.** Hình dạng của nó đến từ một câu hỏi duy nhất mà
RT-10 đặt ra: *dòng này có thể đã tới tay nhà cung cấp chưa?* Câu trả lời phải
đọc được từ trạng thái **đã commit**, vì thứ nó chống đỡ là một tiến trình chết
giữa chừng. Ba cột trả lời: `status`, `in_flight_since` (lease), và
`next_attempt_at`. Lập luận đầy đủ ở `modules/einvoice/outbox.py`.

**Ba ràng buộc mang bất biến, không phải để cho gọn:**

* `UNIQUE (client_ref)` — khóa chống trùng gửi cho nhà cung cấp. Một khóa chống
  trùng bị trùng thì không chống được gì.
* Chỉ mục bán phần `uq_einvoice_outbox_open_operation` — **một dòng còn mở cho
  mỗi (hóa đơn, việc)**. Không có nó, hai lượt bấm "Phát hành" xếp hai dòng với
  hai `client_ref` khác nhau, và hai `client_ref` khác nhau là **hai tờ hóa
  đơn** dưới mắt nhà cung cấp — đúng thứ mà cả đường `needs_reconcile` dựng ra
  để tránh.
* `lease_only_while_in_flight` — đang bay thì phải có lease, và có lease thì
  phải đang bay. Một dòng `in_flight` không lease là dòng không lượt quét nào
  đòi lại được: treo vĩnh viễn, đúng như dòng `jobs` kẹt "đang chạy" mà
  `kernel.jobs.reaper` sinh ra để chặn.

**Khóa ngoại ghép** `(einvoice_id, branch_id)` → `einvoices (id, branch_id)`,
nên revision này thêm `UNIQUE (id, branch_id)` lên `einvoices` làm đích — cùng
thao tác mà `0032` đã làm cho `vouchers`, và không tốn thêm bảo đảm nào vì `id`
đã là khóa chính. Đổi lại, một dòng hàng đợi trỏ sang chi nhánh khác trở thành
**không biểu diễn được** thay vì phải kiểm.

`ON DELETE CASCADE` chứ không `RESTRICT` như khóa ngoại của `einvoices` sang
`vouchers`: xóa được hóa đơn thì nó còn là bản nháp chưa tiêu số
(`einvoices_refuse_numbered_delete` của `0032` canh chiều ấy), mà bản nháp thì
chưa có dòng hàng đợi nào. Dòng duy nhất `CASCADE` có thể cuốn theo là dòng của
một hóa đơn mà tầng trên đã cho phép xóa — giữ lại chỉ tạo rác trỏ vào hư không.

**Không đụng tới `einvoices`** ngoài ràng buộc duy nhất nói trên, nên trigger
`einvoices_immutable_after_issue` giữ nguyên. Bảng này **không** có cột `BYTEA`
nào: XML đã ký của 7E-2 đi qua cơ chế đính kèm content-hash (quyết định user
2026-09-07), và một cột nullable chưa đường ghi nào chạm tới ở lát này là một
cột không ai kiểm được là đúng hay sai.

Không đổi metadata builtin nào nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements, set_search_path_statement

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_CODE_MAX_LENGTH = 50
_PROVIDER_REF_MAX_LENGTH = 100

_OUTBOX_TABLE = "einvoice_outbox"
_EINVOICE_TABLE = "einvoices"
_EINVOICE_BRANCH_UNIQUE = "uq_einvoices_id_branch"


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
    _add_einvoice_branch_unique()
    _create_outbox()
    _apply_security()


def _add_einvoice_branch_unique() -> None:
    """Đích cho khóa ngoại ghép của `einvoice_outbox` — xem docstring đầu tệp."""
    op.create_unique_constraint(_EINVOICE_BRANCH_UNIQUE, _EINVOICE_TABLE, ["id", "branch_id"])


def _create_outbox() -> None:
    table = _OUTBOX_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("einvoice_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("provider_code", sa.String(length=_PROVIDER_CODE_MAX_LENGTH), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("client_ref", sa.Uuid(), nullable=False),
        sa.Column("provider_ref", sa.String(length=_PROVIDER_REF_MAX_LENGTH), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("in_flight_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('in_flight', 'done', 'failed', 'needs_reconcile')",
            name="status_known",
        ),
        sa.CheckConstraint("operation IN ('issue')", name="operation_known"),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_not_negative"),
        sa.CheckConstraint(
            "(status = 'in_flight') = (in_flight_since IS NOT NULL)",
            name="lease_only_while_in_flight",
        ),
        sa.UniqueConstraint("client_ref", name="uq_einvoice_outbox_client_ref"),
        sa.ForeignKeyConstraint(
            ["einvoice_id", "branch_id"],
            [f"{_EINVOICE_TABLE}.id", f"{_EINVOICE_TABLE}.branch_id"],
            name="fk_einvoice_outbox_einvoice",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name="fk_einvoice_outbox_branch", ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_einvoice_outbox_open_operation",
        table,
        ["einvoice_id", "operation"],
        unique=True,
        postgresql_where=sa.text("status <> 'done' AND status <> 'failed'"),
    )
    op.create_index(
        "ix_einvoice_outbox_due",
        table,
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('in_flight', 'needs_reconcile')"),
    )
    op.create_index("ix_einvoice_outbox_einvoice", table, ["einvoice_id"])


def _apply_security() -> None:
    op.execute(set_search_path_statement(_target_schema()))
    grantee = _dataset_grantee()
    for statement in grant_read_write(
        _OUTBOX_TABLE, grantee=grantee, sequence=serial_sequence_name(_OUTBOX_TABLE)
    ):
        op.execute(statement)
    for statement in enable_branch_rls_statements(_OUTBOX_TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(_OUTBOX_TABLE)
    op.drop_constraint(_EINVOICE_BRANCH_UNIQUE, _EINVOICE_TABLE, type_="unique")
