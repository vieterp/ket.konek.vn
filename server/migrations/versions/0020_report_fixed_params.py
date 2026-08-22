"""Tham số ghim của báo cáo — lát 6E-1.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-22

Chạy **một lần cho mỗi schema dataset** như `0001`..`0019`, bằng `ket_owner`.

Một cột: `report_definitions.fixed_params` JSONB NOT NULL DEFAULT `{}` — giá
trị GHIM cho tham số đã khai trong bộ tham số của báo cáo (xem docstring
`ReportDefinition.fixed_params`). Đây là thứ cho hai mẫu sổ khác nhau đúng một
tham số dùng CHUNG một dataset: `S03a1-DN` (Sổ Nhật ký thu tiền) và `S03a2-DN`
(Sổ Nhật ký chi tiền) là cùng câu SQL với `direction` ghim khác nhau — phase 7
dùng lại y hệt cho `S03a3-DN`/`S03a4-DN` (nhật ký mua/bán hàng).

`{}` là mặc định đúng tuyệt đối cho dòng cũ: trước lát này không báo cáo nào
ghim tham số nào, nên không có giá trị nào phải đoán.

Cuối cùng là bước dữ liệu `_refresh_builtin_data` CHUYỂN TỪ 0017 sang (doctrine
lát 6B, giữ nguyên): `refresh_builtin_reports` đọc dữ liệu builtin đóng gói
HIỆN TẠI, gieo lại definition và probe SQL từng dataset — nên nó chỉ chạy đúng
ở migration MỚI NHẤT của chuỗi. Lát này thêm 6 dataset builtin và ghi cột
`fixed_params` vừa tạo bên trên, nên bước phải nằm ở đây chứ không ở 0017.
Migration tương lai thêm cột mà dataset builtin đọc phải dời tiếp bước này về
cuối chuỗi.

`downgrade()` drop cột — chưa có bản cài phát hành.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_fixed_params()
    _refresh_builtin_data()


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _add_fixed_params() -> None:
    op.add_column(
        "report_definitions",
        sa.Column(
            "fixed_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=_target_schema(),
    )


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — CHUYỂN TỪ 0017 (xem docstring
    đầu tệp). Chỉ chạy online, cùng lý do với bản gốc: bước dữ liệu đọc-rồi-ghi
    không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    op.drop_column("report_definitions", "fixed_params", schema=_target_schema())
