"""Bảng đệm nhập liệu Excel và mở rộng danh sách hành vi được ghi vết.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-18

Chạy **một lần cho mỗi schema dataset** như `0001`..`0005`, bằng `ket_owner`.

Hai việc:

1. `import_staging_rows` — dòng thô của một lượt nhập, khóa theo `job_id` (H82).
   **`UNLOGGED`**: nội dung dựng lại được từ tệp đã lưu trong kho định địa chỉ
   theo nội dung, nên nó không đáng nằm trong WAL, không đáng đi vào bản sao lưu,
   và mất sạch sau một lần máy chủ đổ là kết quả đúng.

   Là bảng **thật** chứ không `CREATE TEMP TABLE` (H83), và đó là điểm cần đọc kỹ
   nhất của bản này: `security/roles.sql` thu hồi có chủ đích quyền `TEMPORARY`
   của mọi vai trò runtime, vì `pg_temp` đứng trước mọi schema khác trong đường
   tra — một `CREATE TEMP TABLE audit_log (…)` khiến nhật ký bất biến bốc hơi mà
   không sửa hay xóa dòng nào (RT-02). Cấp lại quyền ấy cho tiện nhập liệu là bán
   đúng bất biến đó. Một bảng có **tên** trong schema dataset thì không che được
   bảng nào.

2. `audit_log.action` nhận thêm `'imported'` (H84). Một lượt nhập ghi **một**
   dòng nhật ký cho cả lượt chứ không 10.000 dòng, vì đường ghi của nó là
   `INSERT … SELECT` tập hợp nằm ngoài hai móc dựng nhật ký. `AuditAction` khai
   danh sách này là hợp đồng có `CHECK` dưới DB canh, nên thêm giá trị bắt buộc
   kèm migration.

**Không** bật RLS, và bảng này nằm **ngoài** tầm cổng `test_rls_policy_coverage`
chứ không phải được miễn trừ trong đó: cổng ấy đòi policy cho mọi bảng **có cột
`branch_id`**, còn bảng này cố ý không có cột đó.

Lý do không có: phạm vi của một dòng đệm là phạm vi của job sinh ra nó, và
`jobs` đã mang `branch_id` của người bấm nút — đó cũng là giá trị mà thân job
dùng cho mọi phép tra cứu. Thêm một `branch_id` thứ hai ở đây là tạo ra hai
nguồn cho cùng một câu trả lời, và một policy đọc ngược lên `jobs` cho mỗi dòng
sẽ đánh vào đúng câu truy vấn tập hợp mà cả thiết kế dựng ra để chạy nhanh.

Điều **thật sự** canh phạm vi ở đây là khóa `job_id` trong mọi truy vấn của
`StagingTable`, cộng với `queue.get_job` — nó chỉ trả về job của dataset và
phạm vi người gọi, nên một `validation_job_id` của người khác không đọc được.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGING = "import_staging_rows"
_AUDIT = "audit_log"

_ACTIONS_BEFORE = (
    "'created', 'updated', 'deleted', 'posted', 'unposted', 'locked', 'unlocked', 'printed'"
)
_ACTIONS_AFTER = f"{_ACTIONS_BEFORE}, 'imported'"
"""Viết ra nguyên văn thay vì sinh từ `AuditAction`: migration là **ảnh chụp
lịch sử**, và đọc enum của mã nguồn sẽ khiến bản này âm thầm đổi nghĩa mỗi lần
phase sau thêm một hành vi."""


def upgrade() -> None:
    _create_staging_table()
    _extend_audit_actions()
    _apply_grants()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _create_staging_table() -> None:
    op.create_table(
        _STAGING,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("cells", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_staging_rows")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_import_staging_rows_job_id_jobs"),
            # `CASCADE` chứ không `RESTRICT` như quy ước của danh mục: một dòng
            # đệm không có giá trị gì khi job của nó biến mất, và `RESTRICT` ở
            # đây biến việc dọn hàng đợi thành việc phải dọn hai bảng đúng thứ tự.
            ondelete="CASCADE",
        ),
    )
    op.create_index(f"ix_{_STAGING}_job", _STAGING, ["job_id", "row_number"])
    # `UNLOGGED` bằng `ALTER` chứ không tham số của `create_table`: Alembic không
    # có tham số nào cho nó, và một `op.execute` với DDL viết tay cho cả bảng sẽ
    # mất phần `op.f()` đặt tên ràng buộc theo quy ước của dự án.
    op.execute("ALTER TABLE import_staging_rows SET UNLOGGED")


def _extend_audit_actions() -> None:
    """Thêm `'imported'` vào ràng buộc `CHECK` của `audit_log.action`.

    Bảng nhật ký thuộc `ket_owner` và vai trò runtime **không** có `ALTER` trên
    nó (RT-02) — migration chạy bằng owner nên lệnh này hợp lệ, còn một đường
    nào đó ở tầng ứng dụng thử làm việc tương tự thì sẽ bị từ chối, đúng như
    thiết kế.
    """
    op.drop_constraint(op.f("ck_audit_log_action_known"), _AUDIT, type_="check")
    op.create_check_constraint(
        op.f("ck_audit_log_action_known"), _AUDIT, f"action IN ({_ACTIONS_AFTER})"
    )


def _apply_grants() -> None:
    """Quyền cho vai trò runtime của schema đang migrate (D3, RT-02).

    Bảng có `SERIAL` id nên phải cấp thêm quyền trên **sequence**: thiếu nó thì
    `INSERT` đầu tiên đổ với `permission denied for sequence`, ở đúng lượt nhập
    liệu đầu tiên của người dùng chứ không ở CI.
    """
    for statement in grant_read_write(
        _STAGING, grantee=_dataset_grantee(), sequence=serial_sequence_name(_STAGING)
    ):
        op.execute(statement)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_audit_log_action_known"), _AUDIT, type_="check")
    op.create_check_constraint(
        op.f("ck_audit_log_action_known"), _AUDIT, f"action IN ({_ACTIONS_BEFORE})"
    )
    op.drop_table(_STAGING)
