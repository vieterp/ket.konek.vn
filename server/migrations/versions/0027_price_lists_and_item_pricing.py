"""Bảng giá, mức giá và bậc chiết khấu — bốn bảng cùng một cột của lát 7C-1.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-04

Chạy **một lần cho mỗi schema dataset** như `0001`..`0026`, bằng `ket_owner`.

Ba tầng nguồn giá của phase 7 (§Chính sách giá & chiết khấu) và bậc chiết khấu:

* `price_lists` — **danh mục thứ 22** (FR-SAL-020): bảng giá theo đối tác/nhóm
  đối tác/hợp đồng, có cửa sổ hiệu lực. Dùng bộ cột chung của danh mục.
* `price_list_lines` — dòng của bảng giá: mã hàng, đơn vị, ngưỡng số lượng, đơn
  giá. **Không** phải danh mục nên không dùng bộ cột chung.
* `item_price_levels` — mức giá mua/bán nhiều mức theo đơn vị, gắn thẳng vào mã
  hàng (FR-SYS-042). Mức 1 với `unit_id IS NULL` **là** "đơn giá mặc định trên
  danh mục", tầng cuối của thứ tự nguồn giá — không có cột `sale_price` riêng.
* `item_discount_tiers` — bậc chiết khấu theo số lượng (FR-SYS-045).
* `items.price_is_tax_inclusive` — vế "cấp từng mặt hàng" của FR-SYS-043. Ba
  trạng thái: `NULL` = theo khóa thiết lập `sales.price_is_tax_inclusive`.

`_master_data_columns`/`_create_master_data_table` lặp lại y như `0003`..`0005`
thay vì import: migration là ảnh chụp lịch sử, và một hàm dùng chung giữa các bản
sẽ đổi hình dạng của bản cũ mỗi lần bản mới cần thêm gì.

**Hai chỉ số duy nhất riêng phần thay cho một `UNIQUE`** ở cả hai bảng có
`unit_id`, tách theo `unit_id IS NULL`: `UNIQUE` thường coi mọi `NULL` là khác
nhau nên nó cho khai vô số dòng "giá theo đơn vị chính" cùng một ô. Cùng cách vá
với `uq_{bảng}_shared_code` của danh mục (`0003`).

**Không** bật RLS cho bảng nào: `price_lists` là danh mục mang `branch_id IS
NULL` = dùng chung (FR-SYS-018) — cùng lập luận `0003`..`0005`; ba bảng còn lại
không có `branch_id`, phạm vi của chúng là phạm vi của bản ghi danh mục chủ. Cả
bốn khai miễn trừ tường minh trong `tests/test_rls_policy_coverage.py`.

**Không** có bước làm mới metadata builtin ở bản này, khác `0025`/`0026`: lát
7C-1 không đổi thứ gì dữ liệu builtin đọc — không thêm báo cáo, không đổi mã
quyền của báo cáo có sẵn, không sửa hệ thống tài khoản. Bước ấy ở lại `0026`.
Mã quyền của danh mục mới (`master.price_lists.*`) không đi qua migration: nó
đến từ registry và được `ensure-cluster` gieo vào **mọi** dataset đã tồn tại
(`bootstrap.seed_registered_datasets`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = 50
_NAME = 255
_PATH = 255
_PRICE_LABEL = 100

_UNIT_PRICE_PRECISION = 24
_UNIT_PRICE_SCALE = 6
_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6
_DISCOUNT_PRECISION = 5
_DISCOUNT_SCALE = 2

_PRICE_LISTS = "price_lists"
_PRICE_LIST_LINES = "price_list_lines"
_ITEM_PRICE_LEVELS = "item_price_levels"
_ITEM_DISCOUNT_TIERS = "item_discount_tiers"

_PRICE_LEVEL_MIN = 1
_PRICE_LEVEL_MAX = 20
"""Trần số mức — bằng `PRICE_LEVEL_MIN/MAX` của `models/item_price_level.py`,
viết ra ở đây vì migration không đi theo mã nguồn về sau."""


def upgrade() -> None:
    _create_price_lists()
    _create_price_list_lines()
    _create_item_price_levels()
    _create_item_discount_tiers()
    _add_tax_inclusive_flag_to_items()
    _extend_group_constraint_on_items()
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


def _create_price_lists() -> None:
    table = _PRICE_LISTS
    _create_master_data_table(
        table,
        # `PriceDirection`: 0 giá mua, 1 giá bán — số trần có chú thích, không
        # import enum (cùng luật đã ghi ở `0022`/`0026`).
        # `NULL` chỉ cho nút nhóm — `direction_set_unless_group` canh.
        sa.Column("direction", sa.SmallInteger(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        # Bảng giá **luôn dùng chung toàn công ty**: danh mục không bật RLS, nên
        # lớp cô lập chi nhánh duy nhất là `_visible_to` ở đường đọc danh mục — mà
        # bộ định giá đọc thẳng bảng này bằng một đường khác. Ràng buộc làm trạng
        # thái "bảng giá riêng chi nhánh" **không biểu diễn được**, thay vì phải
        # nhớ lọc ở mọi đường đọc mới.
        sa.CheckConstraint(
            "branch_id IS NULL", name=op.f(f"ck_{table}_always_shared_company_wide")
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN (0, 1)",
            name=op.f(f"ck_{table}_direction_is_known"),
        ),
        # Nút nhóm chỉ gom cây, không bao giờ được bộ định giá xét tới — cùng
        # khuôn `nature_set_unless_group` của `items` (0005), gồm cả chiều ngược
        # lại: nhóm được **cấm** mang thứ gì của một bảng giá thật, không chỉ được
        # miễn.
        sa.CheckConstraint(
            "is_group OR direction IS NOT NULL",
            name=op.f(f"ck_{table}_direction_set_unless_group"),
        ),
        sa.CheckConstraint(
            "NOT is_group OR (direction IS NULL AND partner_id IS NULL "
            "AND contract_id IS NULL AND effective_from IS NULL AND effective_to IS NULL)",
            name=op.f(f"ck_{table}_group_has_no_pricing_fields"),
        ),
        sa.CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from",
            name=op.f(f"ck_{table}_effective_window_is_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["partners.id"],
            name=op.f(f"fk_{table}_partner_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["contracts.id"],
            name=op.f(f"fk_{table}_contract_id"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(f"ix_{table}_partner_id", table, ["partner_id"])
    op.create_index(f"ix_{table}_contract_id", table, ["contract_id"])


def _create_price_list_lines() -> None:
    table = _PRICE_LIST_LINES
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("price_list_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        sa.Column(
            "min_quantity",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "price",
            sa.Numeric(precision=_UNIT_PRICE_PRECISION, scale=_UNIT_PRICE_SCALE),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("min_quantity > 0", name=op.f(f"ck_{table}_min_quantity_is_positive")),
        sa.CheckConstraint("price >= 0", name=op.f(f"ck_{table}_price_is_not_negative")),
        sa.ForeignKeyConstraint(
            ["price_list_id"],
            [f"{_PRICE_LISTS}.id"],
            name=op.f(f"fk_{table}_price_list_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{table}_item_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["units_of_measure.id"],
            name=op.f(f"fk_{table}_unit_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(
        f"uq_{table}_base_unit",
        table,
        ["price_list_id", "item_id", "min_quantity"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NULL"),
    )
    op.create_index(
        f"uq_{table}_alt_unit",
        table,
        ["price_list_id", "item_id", "unit_id", "min_quantity"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NOT NULL"),
    )
    op.create_index(f"ix_{table}_list_item", table, ["price_list_id", "item_id"])
    op.create_index(f"ix_{table}_item_id", table, ["item_id"])
    op.create_index(f"ix_{table}_unit_id", table, ["unit_id"])


def _create_item_price_levels() -> None:
    table = _ITEM_PRICE_LEVELS
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        # `PriceDirection`: 0 giá mua, 1 giá bán.
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column(
            "price",
            sa.Numeric(precision=_UNIT_PRICE_PRECISION, scale=_UNIT_PRICE_SCALE),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=_PRICE_LABEL), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("price >= 0", name=op.f(f"ck_{table}_price_is_not_negative")),
        sa.CheckConstraint(
            f"level BETWEEN {_PRICE_LEVEL_MIN} AND {_PRICE_LEVEL_MAX}",
            name=op.f(f"ck_{table}_level_within_bounds"),
        ),
        sa.CheckConstraint("direction IN (0, 1)", name=op.f(f"ck_{table}_direction_is_known")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{table}_item_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["units_of_measure.id"],
            name=op.f(f"fk_{table}_unit_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(
        f"uq_{table}_base_unit",
        table,
        ["item_id", "direction", "level"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NULL"),
    )
    op.create_index(
        f"uq_{table}_alt_unit",
        table,
        ["item_id", "unit_id", "direction", "level"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NOT NULL"),
    )
    op.create_index(f"ix_{table}_item_direction", table, ["item_id", "direction"])
    op.create_index(f"ix_{table}_unit_id", table, ["unit_id"])


def _create_item_discount_tiers() -> None:
    table = _ITEM_DISCOUNT_TIERS
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "min_quantity",
            sa.Numeric(precision=_QUANTITY_PRECISION, scale=_QUANTITY_SCALE),
            nullable=False,
        ),
        sa.Column(
            "discount_percent",
            sa.Numeric(precision=_DISCOUNT_PRECISION, scale=_DISCOUNT_SCALE),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("min_quantity > 0", name=op.f(f"ck_{table}_min_quantity_is_positive")),
        sa.CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name=op.f(f"ck_{table}_discount_percent_within_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{table}_item_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("item_id", "min_quantity", name=f"uq_{table}_item_min_quantity"),
    )
    op.create_index(f"ix_{table}_item_id", table, ["item_id"])


def _extend_group_constraint_on_items() -> None:
    """Nhóm vật tư **không được** mang cờ giá sau thuế (review M-1).

    `ck_items_group_carries_no_item_data` (0005) liệt kê ba cột; cột thứ tư thêm ở
    bản này phải vào cùng danh sách, nếu không nhóm được **miễn** chứ không bị
    **cấm** — đúng lỗ hổng mà chính comment cạnh ràng buộc ấy kể lại (review H-1
    của lát 3B-3), chỉ đến bằng một cột khác.

    Bỏ rồi tạo lại vì Postgres không sửa được biểu thức của một `CHECK` tại chỗ.
    """
    # Tên **trần**, không kèm tiền tố: quy ước đặt tên của Alembic (`op.f`) tự
    # thêm `ck_items_`, nên truyền tên đầy đủ sẽ ra `ck_items_ck_items_...`.
    op.drop_constraint("group_carries_no_item_data", "items", type_="check")
    op.create_check_constraint(
        "group_carries_no_item_data",
        "items",
        "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL "
        "AND warehouse_id IS NULL AND price_is_tax_inclusive IS NULL)",
    )


def _add_tax_inclusive_flag_to_items() -> None:
    """Vế "cấp từng mặt hàng" của FR-SYS-043 — ba trạng thái, nên **nullable**.

    Không `server_default`: `NULL` ở đây là một **giá trị có nghĩa** ("theo thiết
    lập hệ thống"), không phải chỗ trống chờ điền, nên mọi mã hàng đã có đúng ra
    phải mang nó — và đó chính là điều `ADD COLUMN` không kèm mặc định làm.
    """
    op.add_column("items", sa.Column("price_is_tax_inclusive", sa.Boolean(), nullable=True))


def _apply_grants() -> None:
    """Quyền cho vai trò runtime, kèm sequence của bốn bảng khóa `SERIAL`."""
    grantee = _dataset_grantee()
    for table in (_PRICE_LISTS, _PRICE_LIST_LINES, _ITEM_PRICE_LEVELS, _ITEM_DISCOUNT_TIERS):
        for statement in grant_read_write(
            table, grantee=grantee, sequence=serial_sequence_name(table, "id")
        ):
            op.execute(statement)


def downgrade() -> None:
    op.drop_constraint("group_carries_no_item_data", "items", type_="check")
    op.drop_column("items", "price_is_tax_inclusive")
    op.create_check_constraint(
        "group_carries_no_item_data",
        "items",
        "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL AND warehouse_id IS NULL)",
    )
    for table in (_ITEM_DISCOUNT_TIERS, _ITEM_PRICE_LEVELS, _PRICE_LIST_LINES, _PRICE_LISTS):
        op.drop_table(table)
