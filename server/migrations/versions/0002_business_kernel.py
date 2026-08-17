"""Kernel nghiệp vụ: cây tổ chức, danh mục, tiền tệ, kỳ kế toán, đánh số.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17

Chạy **một lần cho mỗi schema dataset** như `0001`, bằng vai trò `ket_owner`.

Ba việc, theo thứ tự phụ thuộc:

1. **Mở rộng bảng đã có.** `branches` thành cây (FR-SYS-050) và
   `number_sequences` mang thêm cấu hình đánh số (FR-SYS-063). Cả hai đã có dữ
   liệu ở bản cài đang chạy, nên cột mới phải thêm được mà không mất dòng nào —
   xem `_upgrade_branches`.
2. **Bảng mới.** Danh mục (`cost_objects`, `expense_items` + bộ đếm tham chiếu),
   tiền tệ, kỳ kế toán, sổ cấp số.
3. **Quyền.** Mọi bảng mới cấp cho vai trò runtime của **chính schema đang
   migrate** (D3) ngay cạnh chỗ tạo bảng — `GRANT ON ALL TABLES` ở cuối sẽ bỏ
   sót bảng của migration sau.

**Không** bật RLS cho bảng nào ở đây, có chủ đích: tất cả đều là danh mục hoặc
cấu hình toàn công ty. Với `cost_objects`/`expense_items` thì `branch_id IS NULL`
nghĩa là "dùng chung toàn công ty" (FR-SYS-018) và policy chi nhánh sẽ giấu đúng
những dòng đó khỏi mọi người — cùng lập luận đã áp cho `branches` ở `0001`.
Miễn trừ được khai tường minh trong `tests/test_rls_policy_coverage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = 50
_NAME = 255
_PATH = 255

_NEW_TABLES_WITH_SERIAL_ID = (
    "cost_objects",
    "expense_items",
    "fiscal_years",
    "accounting_periods",
    "allocated_numbers",
)
_NEW_TABLES_WITHOUT_SERIAL_ID = (
    "currencies",
    "exchange_rates",
    "master_data_usage",
)


def upgrade() -> None:
    _upgrade_branches()
    _upgrade_number_sequences()
    _create_master_data_tables()
    _create_currency_tables()
    _create_period_tables()
    _create_allocated_numbers()
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


def _upgrade_branches() -> None:
    """`branches` thành cây, giữ nguyên mọi dòng đang có (FR-SYS-050).

    Thứ tự bắt buộc: thêm cột cho `NULL` → nạp giá trị cho dòng cũ → siết
    `NOT NULL` → mới thêm ràng buộc `CHECK`. Làm ngược lại thì ràng buộc kiểm
    trên những dòng chưa kịp có giá trị và migration đổ ngay tại bản cài đầu
    tiên có dữ liệu — tức là ở khách hàng, không phải ở CI.

    Chi nhánh đang có đều thành **nút gốc** (`path = '<id>.'`, `level = 1`).
    Không đoán cấu trúc trực thuộc: ai là cấp dưới của ai là thông tin nghiệp vụ
    mà chỉ doanh nghiệp biết, và đoán sai thì báo cáo hợp nhất cộng nhầm nhánh.
    """
    op.add_column("branches", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.add_column("branches", sa.Column("path", sa.String(length=_PATH), nullable=True))
    op.add_column("branches", sa.Column("level", sa.Integer(), nullable=True))
    op.add_column(
        "branches",
        sa.Column(
            "is_dependent_accounting",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column("branches", sa.Column("tax_code", sa.String(length=20), nullable=True))
    op.add_column("branches", sa.Column("address", sa.String(length=500), nullable=True))

    backfill = sa.text("UPDATE branches SET path = id || '.', level = :root_level")
    op.execute(backfill.bindparams(root_level=ROOT_LEVEL))

    op.alter_column("branches", "path", nullable=False)
    op.alter_column("branches", "level", nullable=False, server_default=str(ROOT_LEVEL))
    op.create_foreign_key(
        op.f("fk_branches_parent_id"),
        "branches",
        "branches",
        ["parent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("path_is_dotted_ids", "branches", f"path ~ '{PATH_PATTERN}'")
    op.create_check_constraint("level_at_least_root", "branches", f"level >= {ROOT_LEVEL}")
    op.create_index("ix_branches_path", "branches", ["path"])
    # Khóa ngoại `RESTRICT` không tự có chỉ mục ở phía tham chiếu: thiếu nó thì
    # mỗi `DELETE`/`UPDATE` trên `branches` phải quét toàn bảng để kiểm cấp dưới.
    # Bảng danh mục có `ix_{table}_parent_id`; `branches` từng bị bỏ sót (L2).
    op.create_index("ix_branches_parent_id", "branches", ["parent_id"])


def _upgrade_number_sequences() -> None:
    """Thêm cấu hình đánh số lên bộ đếm đã có (FR-SYS-063).

    Cả ba cột đều có `server_default`, nên dòng bộ đếm đang chạy ở bản cài hiện
    tại nhận giá trị hợp lệ ngay mà không cần một lượt nạp riêng.
    """
    op.add_column(
        "number_sequences",
        sa.Column("document_type", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "number_sequences",
        sa.Column("suffix", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "number_sequences",
        sa.Column("reset_rule", sa.String(length=20), nullable=False, server_default="never"),
    )
    op.create_check_constraint(
        "reset_rule_known",
        "number_sequences",
        "reset_rule IN ('never', 'yearly', 'monthly')",
    )
    op.create_check_constraint("padding_in_range", "number_sequences", "padding BETWEEN 1 AND 12")
    op.create_check_constraint("next_value_positive", "number_sequences", "next_value >= 1")


def _master_data_columns() -> list[sa.Column[object]]:
    """Cột dùng chung của bảng danh mục — phản chiếu `MasterDataMixin`.

    Chép cấu trúc chứ không sinh từ metadata của model: migration phải mô tả
    schema **tại thời điểm nó chạy**, không phải schema mà model hiện tại đang
    mô tả. Model đổi ở phase sau thì migration cũ vẫn phải dựng đúng cái schema
    cũ, nếu không thì chuỗi migration không tái lập được. `test_migrations_
    match_models` là chỗ bắt hai bên lệch nhau ở **bản mới nhất**.
    """
    return [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
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


def _create_master_data_table(table: str) -> None:
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


def _create_master_data_tables() -> None:
    _create_master_data_table("cost_objects")
    _create_master_data_table("expense_items")
    op.create_table(
        "master_data_usage",
        sa.Column("entity_type", sa.String(length=63), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("usage_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "usage_count >= 0", name=op.f("ck_master_data_usage_usage_count_not_negative")
        ),
        sa.PrimaryKeyConstraint("entity_type", "entity_id", name=op.f("pk_master_data_usage")),
    )


def _create_currency_tables() -> None:
    op.create_table(
        "currencies",
        sa.Column("code", sa.String(length=3), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("name_en", sa.String(length=100), nullable=True),
        sa.Column("decimal_places", sa.SmallInteger(), nullable=False, server_default="2"),
        sa.Column("is_base", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "decimal_places BETWEEN 0 AND 6", name=op.f("ck_currencies_decimal_places_in_range")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currencies")),
    )
    # Đúng **một** đồng tiền hạch toán: chỉ mục bộ phận chặn vế "hai", còn vế
    # "không có" do `ExchangeRateService.base_currency` chặn lúc chạy (DB không
    # có ràng buộc "ít nhất một dòng").
    op.create_index(
        "uq_currencies_single_base",
        "currencies",
        ["is_base"],
        unique=True,
        postgresql_where=sa.text("is_base"),
    )
    op.create_table(
        "exchange_rates",
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate_type", sa.String(length=20), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.CheckConstraint("rate > 0", name=op.f("ck_exchange_rates_rate_positive")),
        sa.CheckConstraint(
            "rate_type IN ('accounting', 'bank_buy', 'bank_sell')",
            name=op.f("ck_exchange_rates_rate_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currencies.code"],
            name=op.f("fk_exchange_rates_currency_code"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "currency_code", "rate_date", "rate_type", name=op.f("pk_exchange_rates")
        ),
    )


def _create_period_tables() -> None:
    op.create_table(
        "fiscal_years",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("accounting_scheme", sa.String(length=10), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("inventory_valuation_method", sa.String(length=20), nullable=False),
        sa.Column("vat_method", sa.String(length=20), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "end_date > start_date", name=op.f("ck_fiscal_years_date_range_ordered")
        ),
        sa.CheckConstraint(
            "accounting_scheme IN ('TT200', 'TT133')",
            name=op.f("ck_fiscal_years_accounting_scheme_known"),
        ),
        sa.CheckConstraint(
            "inventory_valuation_method IN ('wavg_period', 'wavg_moving', 'fifo', 'specific')",
            name=op.f("ck_fiscal_years_inventory_valuation_method_known"),
        ),
        sa.CheckConstraint(
            "vat_method IN ('deduction', 'direct')", name=op.f("ck_fiscal_years_vat_method_known")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiscal_years")),
        sa.UniqueConstraint("code", name=op.f("uq_fiscal_years_code")),
    )
    op.create_index("ix_fiscal_years_start_date", "fiscal_years", ["start_date"])
    # Hai niên độ không được phủ lên nhau — ràng buộc ở **DB**, không ở ứng dụng
    # (sửa sau review H1). Phép kiểm "đọc rồi chèn" trong `PeriodService` là lời
    # nhắc thân thiện, nhưng hai phiên song song cùng đọc "chưa có gì chồng" rồi
    # cùng chèn thì cả hai đều lọt — đo được: một ngày thuộc hai kỳ, và
    # `resolve_period` trả kết quả tùy đường truy vấn.
    #
    # `EXCLUDE USING gist` trên biểu thức `daterange` dùng `range_ops` có sẵn của
    # PostgreSQL, **không** cần extension `btree_gist` (chỉ cần khi trộn cột vô
    # hướng vào ràng buộc) — quan trọng vì tạo extension đòi quyền superuser, thứ
    # bản cài của khách hàng không có.
    #
    # Viết bằng DDL thô vì Alembic autogenerate không so sánh `EXCLUDE`; khai nó
    # ở model sẽ tạo một khác biệt giả trong `test_migrations_match_models`.
    op.execute(
        "ALTER TABLE fiscal_years ADD CONSTRAINT ex_fiscal_years_no_overlap "
        "EXCLUDE USING gist (daterange(start_date, end_date, '[]') WITH &&)"
    )
    op.create_table(
        "accounting_periods",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fiscal_year_id", sa.Integer(), nullable=False),
        sa.Column("period_no", sa.SmallInteger(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "end_date >= start_date", name=op.f("ck_accounting_periods_date_range_ordered")
        ),
        sa.CheckConstraint(
            "period_no BETWEEN 1 AND 13", name=op.f("ck_accounting_periods_period_no_in_range")
        ),
        sa.CheckConstraint(
            "(locked_at IS NULL) = (locked_by IS NULL)",
            name=op.f("ck_accounting_periods_lock_stamp_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["fiscal_year_id"],
            ["fiscal_years.id"],
            name=op.f("fk_accounting_periods_fiscal_year_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_periods")),
    )
    op.create_index(
        "uq_accounting_periods_year_no",
        "accounting_periods",
        ["fiscal_year_id", "period_no"],
        unique=True,
    )
    op.create_index("ix_accounting_periods_range", "accounting_periods", ["start_date", "end_date"])


def _create_allocated_numbers() -> None:
    op.create_table(
        "allocated_numbers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scope_key", sa.String(length=150), nullable=False),
        sa.Column("number", sa.String(length=100), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "allocated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocated_numbers")),
    )
    op.create_index(
        "uq_allocated_numbers_scope_number",
        "allocated_numbers",
        ["scope_key", "number"],
        unique=True,
    )
    op.create_index("ix_allocated_numbers_document_id", "allocated_numbers", ["document_id"])


def _apply_grants() -> None:
    """Quyền cho vai trò runtime của schema đang migrate (D3, RT-02).

    Không bảng nào ở đây là bảng chỉ-thêm, nên tất cả dùng `grant_read_write`.
    Bảng có `SERIAL`/`BIGSERIAL` phải cấp thêm quyền trên **sequence** — thiếu
    nó thì `INSERT` đầu tiên đổ với `permission denied for sequence`, ở đúng
    thao tác đầu tiên của người dùng chứ không ở CI.
    """
    grantee = _dataset_grantee()
    for table in _NEW_TABLES_WITH_SERIAL_ID:
        for statement in grant_read_write(
            table, grantee=grantee, sequence=serial_sequence_name(table)
        ):
            op.execute(statement)
    for table in _NEW_TABLES_WITHOUT_SERIAL_ID:
        for statement in grant_read_write(table, grantee=grantee):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("allocated_numbers")
    op.drop_table("accounting_periods")
    op.drop_table("fiscal_years")
    op.drop_table("exchange_rates")
    op.drop_table("currencies")
    op.drop_table("master_data_usage")
    op.drop_table("expense_items")
    op.drop_table("cost_objects")

    op.drop_constraint("ck_number_sequences_next_value_positive", "number_sequences")
    op.drop_constraint("ck_number_sequences_padding_in_range", "number_sequences")
    op.drop_constraint("ck_number_sequences_reset_rule_known", "number_sequences")
    op.drop_column("number_sequences", "reset_rule")
    op.drop_column("number_sequences", "suffix")
    op.drop_column("number_sequences", "document_type")

    op.drop_index("ix_branches_path", table_name="branches")
    op.drop_constraint("ck_branches_level_at_least_root", "branches")
    op.drop_constraint("ck_branches_path_is_dotted_ids", "branches")
    op.drop_constraint(op.f("fk_branches_parent_id"), "branches", type_="foreignkey")
    op.drop_column("branches", "address")
    op.drop_column("branches", "tax_code")
    op.drop_column("branches", "is_dependent_accounting")
    op.drop_column("branches", "level")
    op.drop_column("branches", "path")
    op.drop_column("branches", "parent_id")
