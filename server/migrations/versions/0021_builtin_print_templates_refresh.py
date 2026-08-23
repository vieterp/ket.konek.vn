"""Gieo lại mẫu in builtin — lát 6E-2.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-22

Chạy **một lần cho mỗi schema dataset** như `0001`..`0020`, bằng `ket_owner`.

**Migration không có cột nào** — cố ý. Lát 6E-2 chỉ thêm DỮ LIỆU: năm mẫu in
builtin (01-TT, 02-TT, ủy nhiệm chi, giấy báo có, 08a-TT). Mà mẫu builtin chỉ
được gieo ở hai chỗ: lúc cấp dataset mới, và ở bước `_refresh_builtin_data`
của migration mới nhất. Dữ liệu kế toán đã chạy tới `0020` sẽ **không bao giờ**
thấy năm mẫu đó nếu không có migration này — đúng cái bẫy "dataset cũ không
nhận backfill" đã cắn ở lát 6A (doctrine 5B M-1).

Bước dữ liệu vì thế CHUYỂN TỪ 0020 sang đây, giữ nguyên doctrine từ 6B:
`refresh_builtin_reports` đọc dữ liệu builtin đóng gói HIỆN TẠI và probe SQL
từng dataset, nên nó chỉ đúng ở migration CUỐI chuỗi. Migration tương lai thêm
cột mà dataset builtin đọc phải dời tiếp bước này về cuối.

`downgrade()` không xóa mẫu: dòng `print_templates` là dữ liệu người dùng sửa
được (FR-RPT-008) và có thể đã được chọn làm mặc định hoặc sửa nội dung — hạ
phiên bản mã nguồn không phải là lý do để xóa thứ kế toán đã chỉnh. Cùng lập
luận với `downgrade` của bước làm mới báo cáo builtin.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _refresh_builtin_data()


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — CHUYỂN TỪ 0020 (xem docstring
    đầu tệp). Chỉ chạy online, cùng lý do với bản gốc: bước dữ liệu đọc-rồi-ghi
    không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    """Không có gì để hạ: migration này chỉ gieo dữ liệu còn thiếu."""
