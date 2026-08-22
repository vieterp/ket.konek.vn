"""Tham số ghim của báo cáo — lát 6E-1.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-22

Chạy **một lần cho mỗi schema dataset** như `0001`..`0019`, bằng `ket_owner`.

Hai cột. (1) `report_definitions.fixed_params` JSONB NOT NULL DEFAULT `{}` — giá
trị GHIM cho tham số đã khai trong bộ tham số của báo cáo (xem docstring
`ReportDefinition.fixed_params`). Đây là thứ cho hai mẫu sổ khác nhau đúng một
tham số dùng CHUNG một dataset: `S03a1-DN` (Sổ Nhật ký thu tiền) và `S03a2-DN`
(Sổ Nhật ký chi tiền) là cùng câu SQL với `direction` ghim khác nhau — phase 7
dùng lại y hệt cho `S03a3-DN`/`S03a4-DN` (nhật ký mua/bán hàng).

`{}` là mặc định đúng tuyệt đối cho dòng cũ: trước lát này không báo cáo nào
ghim tham số nào, nên không có giá trị nào phải đoán.

(2) `report_definitions.required_permission_module` TEXT NULL — phân hệ mà
người đọc phải có ít nhất một quyền `view` trong đó (review 6E-1 H-1b: trước
lát này mọi báo cáo chỉ đòi `reporting.report.view`, nên báo cáo đối chiếu sẽ
là đường đọc `bank_statement_lines` đầu tiên không đi qua quyền ngân hàng).
`NULL` = không cổng phụ, đúng thế đứng cũ của bộ sổ tổng hợp.

Bước dữ liệu `_refresh_builtin_data` từng ở đây (chuyển 0017 → 0020 ở lát 6E-1)
đã CHUYỂN TIẾP sang `0021`: `refresh_builtin_reports` đọc dữ liệu builtin đóng
gói HIỆN TẠI, gieo lại definition và probe SQL từng dataset, nên nó chỉ chạy
đúng ở migration MỚI NHẤT của chuỗi. Sáu dataset builtin thêm ở lát này vẫn
được gieo — chỉ là ở bước cuối chuỗi, một migration sau. Migration tương lai
thêm cột mà dataset builtin đọc phải dời tiếp bước đó về cuối chuỗi.

`downgrade()` drop cột — chưa có bản cài phát hành.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_fixed_params()
    _add_required_permission_module()


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


def _add_required_permission_module() -> None:
    """Cổng quyền phụ của một báo cáo (review 6E-1 H-1b). `NULL` = không cổng —
    đúng thế đứng của mọi báo cáo trước lát này, nên dòng cũ không phải đoán."""
    op.add_column(
        "report_definitions",
        sa.Column("required_permission_module", sa.String(length=50), nullable=True),
        schema=_target_schema(),
    )


def downgrade() -> None:
    op.drop_column("report_definitions", "required_permission_module", schema=_target_schema())
    op.drop_column("report_definitions", "fixed_params", schema=_target_schema())
