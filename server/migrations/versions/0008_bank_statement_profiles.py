"""Hồ sơ định dạng sao kê ngân hàng (RT-26).

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-18

Chạy **một lần cho mỗi schema dataset** như `0001`..`0007`, bằng `ket_owner`.

Bảng này là câu trả lời của plan.md cho RT-26 (rủi ro High): khung nhập liệu
Excel cố ý **strict** — tệp đổi tên sheet hay tên cột thì không nhận (FR-SYS-082)
— trong khi sao kê ngân hàng không theo mẫu nào của Konek. Mỗi nhà băng một
định dạng, và ép chúng vào khung strict là một mâu thuẫn thiết kế. Hồ sơ ở đây
mô tả **cách đọc** tệp của từng ngân hàng, do vai trò quản trị khai một lần.

Sáu ràng buộc `CHECK`, và mỗi cái chặn một hồ sơ *đọc được nhưng sai* — xem
`kernel/bank_import/profile_models._profile_table_args` để biết từng cái chặn
điều gì. Đáng chú ý nhất là hai cái cuối: dấu phần nghìn trùng dấu thập phân,
hoặc dấu ngăn cột CSV trùng một trong hai, đều làm mọi con số của tệp sai **im
lặng** thay vì làm lượt nhập đổ.

**Không** bật RLS, và nằm ngoài tầm cổng `test_rls_policy_coverage` chứ không
được miễn trừ trong đó: cổng ấy đòi policy cho mọi bảng **có cột `branch_id`**,
còn định dạng tệp là tính chất của ngân hàng chứ không của chi nhánh doanh
nghiệp, nên bảng cố ý không có cột đó.

Module Bank của phase 6 là nơi đọc bảng này để nhập sao kê thật (FR-BNK-032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROFILES = "bank_statement_profiles"
_BANKS = "banks"

_NAME_MAX = 100
_COLUMN_NAME_MAX = 100
_DATE_FORMAT_MAX = 40

_FILE_KINDS = "'xlsx', 'csv', 'mt940'"
_SIGN_RULES = "'signed', 'debit_positive'"
"""Viết nguyên văn thay vì sinh từ `StatementFileKind`/`AmountSignRule`, cùng
lý do đã ghi ở `0006`/`0007`: migration là **ảnh chụp lịch sử**, và đọc enum của
mã nguồn sẽ khiến bản này âm thầm đổi nghĩa mỗi lần phase sau thêm một giá trị."""


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def upgrade() -> None:
    op.create_table(
        _PROFILES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bank_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=_NAME_MAX), nullable=False),
        sa.Column("file_kind", sa.String(length=10), nullable=False),
        sa.Column("header_row", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("date_col", sa.String(length=_COLUMN_NAME_MAX), nullable=False),
        sa.Column("date_format", sa.String(length=_DATE_FORMAT_MAX), nullable=False),
        sa.Column("debit_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("credit_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("amount_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("sign_rule", sa.String(length=20), nullable=True),
        sa.Column("ref_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("description_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("balance_col", sa.String(length=_COLUMN_NAME_MAX), nullable=True),
        sa.Column("decimal_sep", sa.String(length=1), nullable=False, server_default="."),
        sa.Column("thousand_sep", sa.String(length=1), nullable=True),
        sa.Column("csv_delimiter", sa.String(length=1), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_PROFILES}")),
        sa.ForeignKeyConstraint(
            ["bank_id"],
            [f"{_BANKS}.id"],
            name=op.f(f"fk_{_PROFILES}_bank_id_{_BANKS}"),
            # `RESTRICT` theo quy ước của danh mục: xóa một ngân hàng còn hồ sơ
            # sẽ bỏ lại cấu hình mồ côi mà không màn hình nào hiện ra.
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("bank_id", "name", name=f"uq_{_PROFILES}_bank_name"),
        sa.CheckConstraint(
            f"file_kind IN ({_FILE_KINDS})", name=op.f(f"ck_{_PROFILES}_file_kind_known")
        ),
        sa.CheckConstraint(
            f"sign_rule IS NULL OR sign_rule IN ({_SIGN_RULES})",
            name=op.f(f"ck_{_PROFILES}_sign_rule_known"),
        ),
        sa.CheckConstraint("header_row >= 1", name=op.f(f"ck_{_PROFILES}_header_row_positive")),
        sa.CheckConstraint(
            "(amount_col IS NOT NULL AND sign_rule IS NOT NULL "
            " AND debit_col IS NULL AND credit_col IS NULL)"
            " OR (amount_col IS NULL AND sign_rule IS NULL"
            "     AND (debit_col IS NOT NULL OR credit_col IS NOT NULL))",
            name=op.f(f"ck_{_PROFILES}_amount_shape_is_one_of_two"),
        ),
        sa.CheckConstraint(
            "decimal_sep <> thousand_sep", name=op.f(f"ck_{_PROFILES}_separators_differ")
        ),
        sa.CheckConstraint(
            "csv_delimiter IS NULL"
            " OR (csv_delimiter <> decimal_sep"
            "     AND (thousand_sep IS NULL OR csv_delimiter <> thousand_sep))",
            name=op.f(f"ck_{_PROFILES}_csv_delimiter_distinct"),
        ),
    )
    for statement in grant_read_write(
        _PROFILES, grantee=_dataset_grantee(), sequence=serial_sequence_name(_PROFILES)
    ):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(_PROFILES)
