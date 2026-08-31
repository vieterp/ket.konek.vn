"""Cờ phạm vi công ty cho báo cáo + gieo lại metadata builtin — lát 6G-2.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-31

Chạy **một lần cho mỗi schema dataset** như `0001`..`0022`, bằng `ket_owner`.

Một cột duy nhất: `report_definitions.requires_full_branch_scope` (M-4). Báo
cáo `doi-chieu-ngan-hang` so **hai vế lệch phạm vi** — dòng sao kê là dữ liệu
mức tài khoản (tài khoản ngân hàng dùng chung không mang chi nhánh) còn vế
chứng từ nằm dưới RLS chi nhánh — nên người được cấp một chi nhánh đọc ra phần
lệch phình đúng bằng phần RLS giấu đi. Cờ này chặn cửa đó bằng 403 có lý do.

Bước dữ liệu `_refresh_builtin_data` CHUYỂN TỪ 0022 sang đây, đúng doctrine từ
6B (`refresh_builtin_reports` đọc dữ liệu builtin đóng gói HIỆN TẠI và probe
SQL từng dataset ⇒ chỉ đúng ở migration CUỐI chuỗi). Không có bước này thì
dataset đã chạy tới 0022 **không bao giờ** thấy hai thay đổi dữ liệu của lát
6G-2 — cờ trên `doi-chieu-ngan-hang` và cổng `general_ledger` mới của `F01-DNN`
(M-7) — đúng cái bẫy "dataset cũ không nhận backfill" đã cắn ở 6A và 6G-1.

Thứ tự trong `upgrade` vì thế bắt buộc: cột trước, gieo lại sau. `refresh` ghi
giá trị cờ vào chính cột này, nên đảo lại là lỗi "column does not exist".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_full_branch_scope_flag()
    _refresh_builtin_data()


def _add_full_branch_scope_flag() -> None:
    """`server_default` false + NOT NULL: mọi báo cáo đang có đều là báo cáo
    một-vế (chạy hẹp thì ra ít dòng hơn, không ra số sai), nên mặc định đúng là
    "không đòi". Bật cờ là việc của từng báo cáo, khai trong metadata."""
    op.add_column(
        "report_definitions",
        sa.Column(
            "requires_full_branch_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — CHUYỂN TỪ 0022 (xem docstring
    đầu tệp). Chỉ chạy online, cùng lý do với bản gốc: bước dữ liệu đọc-rồi-ghi
    không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.drop_column("report_definitions", "requires_full_branch_scope")
