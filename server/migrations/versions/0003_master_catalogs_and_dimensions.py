"""Danh mục đầy đủ (nhóm cây thuần) và chiều phân tích mở rộng.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-17

Chạy **một lần cho mỗi schema dataset** như `0001`/`0002`, bằng `ket_owner`.

Hai nhóm bảng:

1. **Mười lăm danh mục** dùng đúng bộ cột chung của `MasterDataRow`. Mười một
   cái thuần cây; bốn cái mang thêm cột riêng (`asset_types`, `tool_types`,
   `payment_terms`, `banks`) mà lát 3B-2 sẽ đọc — quyết định H50 giới hạn cột
   riêng ở đúng những thứ đã có nơi đọc.
2. **Chiều phân tích mở rộng**: `analysis_dimensions` +
   `analysis_dimension_values` (LD-08, FR-SYS-051).

Hàm `_create_master_data_table` lặp lại ở đây thay vì import từ `0002`: migration
là **ảnh chụp lịch sử**, và một hàm dùng chung giữa hai bản sẽ đổi hình dạng của
bản cũ mỗi lần bản mới cần thêm gì. Đó đúng là kiểu thay đổi làm hai bản cài
cùng số revision mà khác schema.

**Không** bật RLS cho bảng nào, cùng lập luận đã ghi ở `0002`: tất cả là danh
mục hoặc cấu hình toàn công ty, và `branch_id IS NULL` nghĩa là "dùng chung"
(FR-SYS-018) — đúng những dòng mà policy chi nhánh sẽ giấu khỏi mọi người. Miễn
trừ khai tường minh trong `tests/test_rls_policy_coverage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = 50
_NAME = 255
_PATH = 255
_ACCOUNT_PREFIX = 20
_SHORT_NAME = 50
_SWIFT_MAX = 11

_PLAIN_CATALOGS = (
    "projects",
    "project_types",
    "contracts",
    "warehouses",
    "units_of_measure",
    "timekeeping_symbols",
    "document_types",
    "invoice_forms",
    "pit_tables",
    "excise_tax_tables",
    "resource_tax_tables",
)
"""Danh mục dùng **đúng** bộ cột chung, không thêm gì."""

_CATALOGS_WITH_EXTRA_COLUMNS = ("asset_types", "tool_types", "payment_terms", "banks")

_DIMENSION_TABLES = ("analysis_dimensions", "analysis_dimension_values")


def upgrade() -> None:
    for table in _PLAIN_CATALOGS:
        _create_master_data_table(table)
    _create_asset_types()
    _create_tool_types()
    _create_payment_terms()
    _create_banks()
    _create_dimension_tables()
    _apply_grants()


def _dataset_grantee() -> str:
    """Vai trò nhận quyền trên bảng của schema đang migrate (D3).

    Lấy schema từ `Config.attributes` chứ không hỏi DB, để chế độ offline
    (`alembic upgrade --sql`) vẫn sinh được SQL — lý do đầy đủ ở `0001`.
    """
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _master_data_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=_CODE), nullable=False),
        sa.Column("name", sa.String(length=_NAME), nullable=False),
        sa.Column("name_en", sa.String(length=_NAME), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.String(length=_PATH), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default=str(ROOT_LEVEL)),
        sa.Column("is_group", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    ]


def _create_master_data_table(table: str, *extra: sa.schema.SchemaItem) -> None:
    op.create_table(
        table,
        *_master_data_columns(),
        sa.CheckConstraint(f"path ~ '{PATH_PATTERN}'", name=op.f(f"ck_{table}_path_is_dotted_ids")),
        sa.CheckConstraint(f"level >= {ROOT_LEVEL}", name=op.f(f"ck_{table}_level_at_least_root")),
        sa.CheckConstraint("code <> ''", name=op.f(f"ck_{table}_code_not_blank")),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f(f"fk_{table}_branch_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], [f"{table}.id"], name=op.f(f"fk_{table}_parent_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("uid", name=op.f(f"uq_{table}_uid")),
        *extra,
    )
    # Hai chỉ mục duy nhất chứ không một (BR-SYS-01): PostgreSQL coi mọi NULL là
    # khác nhau, nên `UNIQUE (branch_id, code)` đơn thuần cho phép vô số danh mục
    # dùng chung trùng mã — đúng thứ ràng buộc đó sinh ra để chặn.
    op.create_index(
        f"uq_{table}_shared_code",
        table,
        ["code"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NULL"),
    )
    op.create_index(
        f"uq_{table}_branch_code",
        table,
        ["branch_id", "code"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NOT NULL"),
    )
    op.create_index(f"ix_{table}_path", table, ["path"])
    op.create_index(f"ix_{table}_parent_id", table, ["parent_id"])
    op.create_index(op.f(f"ix_{table}_branch_id"), table, ["branch_id"])


def _create_asset_types() -> None:
    _create_master_data_table(
        "asset_types",
        sa.Column("default_useful_life_months", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "default_useful_life_months IS NULL OR default_useful_life_months > 0",
            name=op.f("ck_asset_types_default_useful_life_months_positive"),
        ),
    )


def _create_tool_types() -> None:
    _create_master_data_table(
        "tool_types",
        sa.Column("default_allocation_months", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "default_allocation_months IS NULL OR default_allocation_months > 0",
            name=op.f("ck_tool_types_default_allocation_months_positive"),
        ),
    )


def _create_payment_terms() -> None:
    _create_master_data_table(
        "payment_terms",
        sa.Column("due_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "discount_percent", sa.Numeric(precision=7, scale=4), nullable=False, server_default="0"
        ),
        sa.CheckConstraint("due_days >= 0", name=op.f("ck_payment_terms_due_days_not_negative")),
        sa.CheckConstraint(
            "discount_days >= 0", name=op.f("ck_payment_terms_discount_days_not_negative")
        ),
        sa.CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name=op.f("ck_payment_terms_discount_percent_in_range"),
        ),
        sa.CheckConstraint(
            "discount_days <= due_days",
            name=op.f("ck_payment_terms_discount_window_within_due"),
        ),
    )


def _create_banks() -> None:
    _create_master_data_table(
        "banks",
        sa.Column("short_name", sa.String(length=_SHORT_NAME), nullable=True),
        sa.Column("swift_code", sa.String(length=_SWIFT_MAX), nullable=True),
        sa.CheckConstraint("short_name <> ''", name=op.f("ck_banks_short_name_not_blank")),
        sa.CheckConstraint(
            "swift_code IS NULL OR length(swift_code) IN (8, 11)",
            name=op.f("ck_banks_swift_code_length_iso9362"),
        ),
    )


def _create_dimension_tables() -> None:
    op.create_table(
        "analysis_dimensions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=_CODE), nullable=False),
        sa.Column("name", sa.String(length=_NAME), nullable=False),
        sa.Column("name_en", sa.String(length=_NAME), nullable=True),
        sa.Column("value_source", sa.String(length=20), nullable=False, server_default="list"),
        sa.Column("master_slug", sa.String(length=_CODE), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "applies_to_accounts",
            sa.dialects.postgresql.ARRAY(sa.String(length=_ACCOUNT_PREFIX)),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("code <> ''", name=op.f("ck_analysis_dimensions_code_not_blank")),
        sa.CheckConstraint(
            "(value_source = 'master') = (master_slug IS NOT NULL)",
            name=op.f("ck_analysis_dimensions_master_slug_iff_master_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_dimensions")),
        sa.UniqueConstraint("code", name="uq_analysis_dimensions_code"),
    )

    op.create_table(
        "analysis_dimension_values",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dimension_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=_CODE), nullable=False),
        sa.Column("name", sa.String(length=_NAME), nullable=False),
        sa.Column("name_en", sa.String(length=_NAME), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.String(length=_PATH), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default=str(ROOT_LEVEL)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("code <> ''", name=op.f("ck_analysis_dimension_values_code_not_blank")),
        sa.CheckConstraint(
            f"path ~ '{PATH_PATTERN}'",
            name=op.f("ck_analysis_dimension_values_path_is_dotted_ids"),
        ),
        sa.CheckConstraint(
            f"level >= {ROOT_LEVEL}", name=op.f("ck_analysis_dimension_values_level_at_least_root")
        ),
        sa.ForeignKeyConstraint(
            ["dimension_id"],
            ["analysis_dimensions.id"],
            name=op.f("fk_analysis_dimension_values_dimension_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["analysis_dimension_values.id"],
            name=op.f("fk_analysis_dimension_values_parent_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_dimension_values")),
        sa.UniqueConstraint("dimension_id", "code", name="uq_analysis_dimension_values_code"),
    )
    op.create_index(
        "ix_analysis_dimension_values_path", "analysis_dimension_values", ["dimension_id", "path"]
    )
    # Khóa ngoại `RESTRICT` không có chỉ mục thì mỗi `DELETE` trên bảng cha quét
    # toàn bảng con để kiểm — bài học L2 của lát 3A.
    op.create_index(
        "ix_analysis_dimension_values_parent_id", "analysis_dimension_values", ["parent_id"]
    )


def _apply_grants() -> None:
    """Quyền cho vai trò runtime của schema đang migrate (D3, RT-02).

    Mọi bảng ở đây có `SERIAL` id nên đều phải cấp thêm quyền trên **sequence**:
    thiếu nó thì `INSERT` đầu tiên đổ với `permission denied for sequence`, ở
    đúng thao tác đầu tiên của người dùng chứ không ở CI.
    """
    grantee = _dataset_grantee()
    for table in (*_PLAIN_CATALOGS, *_CATALOGS_WITH_EXTRA_COLUMNS, *_DIMENSION_TABLES):
        for statement in grant_read_write(
            table, grantee=grantee, sequence=serial_sequence_name(table)
        ):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("analysis_dimension_values")
    op.drop_table("analysis_dimensions")
    for table in (*_CATALOGS_WITH_EXTRA_COLUMNS, *_PLAIN_CATALOGS):
        op.drop_table(table)
