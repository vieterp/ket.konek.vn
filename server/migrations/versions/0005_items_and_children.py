"""Vật tư hàng hóa cùng đơn vị quy đổi và mã quy cách.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18

Chạy **một lần cho mỗi schema dataset** như `0001`..`0004`, bằng `ket_owner`.

Ba bảng:

1. `items` — danh mục vật tư hàng hóa (FR-SYS-040). Tính chất (`nature`) quyết
   định hành vi tồn kho và hạch toán; đơn vị chính là gốc quy đổi của mọi số tồn.
2. `item_units` — đơn vị quy đổi, phẳng về đơn vị chính (FR-SYS-041). **Không**
   phải danh mục nên không dùng bộ cột chung.
3. `item_variants` — mã quy cách (FR-SYS-046). Có ở lát nền dù là SHOULD vì nó là
   một **trục khóa** của bảng tồn kho ở phase 8, thứ LD-09 nói không thêm sau được.

`_create_master_data_table` lặp lại y như `0003`/`0004` thay vì import: migration
là ảnh chụp lịch sử, và một hàm dùng chung giữa các bản sẽ đổi hình dạng của bản
cũ mỗi lần bản mới cần thêm gì.

**Không** bật RLS cho bảng nào, cùng lập luận `0002`..`0004`: danh mục dùng chung
mang `branch_id IS NULL` (FR-SYS-018) và policy chi nhánh sẽ giấu đúng những dòng
đó. Hai bảng con không có `branch_id` — phạm vi của chúng là phạm vi của mã hàng
chủ. Miễn trừ khai tường minh trong `tests/test_rls_policy_coverage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = 50
_NAME = 255
_PATH = 255
_DESCRIPTION = 1000
_NATURE = 20
_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6

_ITEMS = "items"
_ITEM_UNITS = "item_units"
_ITEM_VARIANTS = "item_variants"

_INVENTORY_NATURES = "'finished_goods', 'goods'"
"""Tính chất **có** theo dõi tồn kho — bằng `INVENTORY_NATURES` của
`models/item.py`, viết ra ở đây vì migration không đi theo mã nguồn về sau."""


def upgrade() -> None:
    _create_items()
    _create_item_units()
    _create_item_variants()
    _apply_grants()


def _dataset_grantee() -> str:
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


def _create_items() -> None:
    _create_master_data_table(
        _ITEMS,
        # `VARCHAR` + `Enum(native_enum=False)` ở tầng ORM, không kiểu `ENUM`
        # native: thêm một tính chất về sau đòi `ALTER TYPE`, lệnh không chạy được
        # trong cùng transaction với phần còn lại của một migration.
        sa.Column("nature", sa.String(length=_NATURE), nullable=True),
        sa.Column("base_unit_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=_DESCRIPTION), nullable=True),
        sa.CheckConstraint(
            "is_group OR nature IS NOT NULL", name=op.f(f"ck_{_ITEMS}_nature_set_unless_group")
        ),
        # Chiều ngược lại: nhóm **cấm** mang dữ liệu của một mã hàng thật. Thiếu
        # nó thì ràng buộc trên chỉ **miễn** cho nhóm, và một nhóm mang
        # `nature = 'goods'` sẽ lọt vào mọi truy vấn tồn kho của phase 8.
        sa.CheckConstraint(
            "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL AND warehouse_id IS NULL)",
            name=op.f(f"ck_{_ITEMS}_group_carries_no_item_data"),
        ),
        sa.CheckConstraint(
            f"is_group OR nature NOT IN ({_INVENTORY_NATURES}) OR base_unit_id IS NOT NULL",
            name=op.f(f"ck_{_ITEMS}_stock_item_needs_base_unit"),
        ),
        sa.CheckConstraint(
            f"warehouse_id IS NULL OR nature IN ({_INVENTORY_NATURES})",
            name=op.f(f"ck_{_ITEMS}_default_warehouse_needs_stock_nature"),
        ),
        # `RESTRICT` cho cả hai: đơn vị tính và kho là danh mục dùng chung, xóa
        # chúng khi còn mã hàng trỏ tới là làm mất đơn vị đo của số tồn.
        sa.ForeignKeyConstraint(
            ["base_unit_id"],
            ["units_of_measure.id"],
            name=op.f(f"fk_{_ITEMS}_base_unit_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f(f"fk_{_ITEMS}_warehouse_id"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(f"ix_{_ITEMS}_base_unit_id", _ITEMS, ["base_unit_id"])
    op.create_index(f"ix_{_ITEMS}_warehouse_id", _ITEMS, ["warehouse_id"])
    op.create_index(f"ix_{_ITEMS}_nature", _ITEMS, ["nature"])


def _create_item_units() -> None:
    op.create_table(
        _ITEM_UNITS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=False),
        sa.Column(
            "factor",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        # Tỷ lệ 0 biến mọi số lượng thành 0; tỷ lệ âm biến nhập thành xuất.
        sa.CheckConstraint("factor > 0", name=op.f(f"ck_{_ITEM_UNITS}_factor_is_positive")),
        # `CASCADE` theo mã hàng: đơn vị quy đổi thuộc về mã hàng, không tồn tại
        # độc lập. `RESTRICT` theo đơn vị tính: nó là danh mục dùng chung.
        sa.ForeignKeyConstraint(
            ["item_id"],
            [f"{_ITEMS}.id"],
            name=op.f(f"fk_{_ITEM_UNITS}_item_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["units_of_measure.id"],
            name=op.f(f"fk_{_ITEM_UNITS}_unit_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_ITEM_UNITS}")),
        # Một đơn vị khai hai lần cho cùng mã hàng là hai tỷ lệ cho cùng phép quy
        # đổi. Ràng buộc này chạm **cả hai** danh mục, nên cả `items` lẫn
        # `units_of_measure` phải khai `MergeHook` (H70).
        sa.UniqueConstraint("item_id", "unit_id", name=f"uq_{_ITEM_UNITS}_item_unit"),
    )
    op.create_index(f"ix_{_ITEM_UNITS}_item_id", _ITEM_UNITS, ["item_id"])
    op.create_index(f"ix_{_ITEM_UNITS}_unit_id", _ITEM_UNITS, ["unit_id"])


def _create_item_variants() -> None:
    op.create_table(
        _ITEM_VARIANTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=_CODE), nullable=False),
        sa.Column("name", sa.String(length=_NAME), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("code <> ''", name=op.f(f"ck_{_ITEM_VARIANTS}_code_not_blank")),
        sa.CheckConstraint("name <> ''", name=op.f(f"ck_{_ITEM_VARIANTS}_name_not_blank")),
        sa.ForeignKeyConstraint(
            ["item_id"],
            [f"{_ITEMS}.id"],
            name=op.f(f"fk_{_ITEM_VARIANTS}_item_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_ITEM_VARIANTS}")),
        # Duy nhất **trong một mã hàng**: "Đỏ" của áo và "Đỏ" của mũ là hai dòng
        # độc lập.
        sa.UniqueConstraint("item_id", "code", name=f"uq_{_ITEM_VARIANTS}_item_code"),
    )
    op.create_index(f"ix_{_ITEM_VARIANTS}_item_id", _ITEM_VARIANTS, ["item_id"])


def _apply_grants() -> None:
    """Quyền cho vai trò runtime của schema đang migrate (D3, RT-02).

    Cả ba bảng có `SERIAL` id nên phải cấp thêm quyền trên **sequence**: thiếu nó
    thì `INSERT` đầu tiên đổ với `permission denied for sequence`, ở đúng thao tác
    đầu tiên của người dùng chứ không ở CI.
    """
    grantee = _dataset_grantee()
    for table in (_ITEMS, _ITEM_UNITS, _ITEM_VARIANTS):
        for statement in grant_read_write(
            table, grantee=grantee, sequence=serial_sequence_name(table)
        ):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table(_ITEM_VARIANTS)
    op.drop_table(_ITEM_UNITS)
    op.drop_table(_ITEMS)
