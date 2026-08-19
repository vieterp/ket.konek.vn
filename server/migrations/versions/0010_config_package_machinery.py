"""Máy móc quanh gói cấu hình: TK ngầm định, cặp kết chuyển, đổi chế độ kế toán.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-19

Chạy **một lần cho mỗi schema dataset** như `0001`..`0009`, bằng `ket_owner`.

Ba việc, cả ba đều xoay quanh `config_packages`/`chart_of_accounts` đã có từ
`0009` (hình dạng bảng, không đổi cột):

1. **Đổi chế độ kế toán TT200 → TT99** (quyết định người dùng 2026-08-19): TT
   99/2025/TT-BTC thay thế TT200/2014 (Điều 31) cho năm tài chính bắt đầu
   từ/sau 01/01/2026; TT133 không đổi. Hai `CHECK` liệt kê giá trị đổi sang
   `('TT99', 'TT133')` — `ck_fiscal_years_accounting_scheme_known` (tạo ở
   `0002`) và `ck_config_packages_scheme_known` (tạo ở `0009`). Không có dữ
   liệu sản xuất nào tồn tại (bản cài chưa phát hành), nên `upgrade()` không
   cần bước chuyển đổi dữ liệu — chỉ đổi ràng buộc. **`downgrade()` thì có**,
   và phải phân biệt hai loại dữ liệu:
   - `config_packages`/`chart_of_accounts` scheme=TT99 — dữ liệu **gieo tự
     động** (`ensure_builtin_packages`, chạy ngay sau migration này lúc
     `provision_dataset`). Xóa vô hại: gieo lại được nguyên vẹn bất cứ lúc nào.
     `downgrade()` dọn sạch trước khi áp lại `CHECK` cũ.
   - `fiscal_years.accounting_scheme='TT99'` — **quyết định của người dùng**
     (chọn chế độ kế toán cho một năm tài chính, thường đã có chứng từ theo
     sau). Không phải dữ liệu dựng sẵn, không được xóa ngầm. `downgrade()`
     **fail-closed**: từ chối hạ cấp (ném lỗi rõ ràng) nếu còn năm tài chính
     nào dùng TT99, thay vì xóa hoặc để `CheckViolation` thô của PostgreSQL
     đổ ra giữa chừng (sửa sau review, H1).
2. `default_accounts` (FR-SYS-024) — TK ngầm định theo mục đích, khóa bằng
   `(package_id, document_type, purpose)`. `document_type = '*'` là mục đích
   dùng chung mọi loại chứng từ.
3. `closing_account_pairs` (FR-SYS-023, FR-GLE-022) — cặp TK kết chuyển cuối
   kỳ theo thứ tự chạy (`sequence`).

Cả hai bảng mới đều là **cấu hình dùng chung toàn dataset** như
`config_packages`/`chart_of_accounts` (0009 §RLS chi nhánh): không mang
`branch_id`, không bật RLS — chỉ cấp quyền đọc/ghi cho vai trò runtime.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_SCHEME_VALUES = "'TT99', 'TT133'"
_OLD_SCHEME_VALUES = "'TT200', 'TT133'"

_GRANTS: tuple[tuple[str, str | None], ...] = (
    ("default_accounts", None),
    ("closing_account_pairs", serial_sequence_name("closing_account_pairs")),
)


def upgrade() -> None:
    _swap_scheme_constraints()
    _create_default_accounts()
    _create_closing_account_pairs()
    _apply_security()


def _dataset_grantee() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return role_name_for_schema(schema)


def _swap_scheme_constraints() -> None:
    """`TT200` → `TT99` trên cả hai `CHECK` liệt kê chế độ kế toán.

    Xóa rồi tạo lại cùng tên **logic** (chưa mang tiền tố `ck_<bảng>_` — đúng
    quy ước `0007`: Alembic tự áp `NAMING_CONVENTION` lên tên logic cho cả
    `create_check_constraint` lẫn `drop_constraint`, nên truyền tên **đã**
    mang tiền tố vào `drop_constraint` sẽ bị áp tiền tố **lần hai**
    (`ck_fiscal_years_ck_fiscal_years_…`) và đổ `UndefinedObject` lúc chạy).
    Tên cuối cùng trong DB không đổi: vẫn là `ck_fiscal_years_accounting_scheme_known`
    / `ck_config_packages_scheme_known`.
    """
    op.drop_constraint("accounting_scheme_known", "fiscal_years", type_="check")
    op.create_check_constraint(
        "accounting_scheme_known", "fiscal_years", f"accounting_scheme IN ({_NEW_SCHEME_VALUES})"
    )
    op.drop_constraint("scheme_known", "config_packages", type_="check")
    op.create_check_constraint(
        "scheme_known", "config_packages", f"scheme IN ({_NEW_SCHEME_VALUES})"
    )


def _create_default_accounts() -> None:
    op.create_table(
        "default_accounts",
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("account_code", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "document_type <> ''", name=op.f("ck_default_accounts_document_type_not_blank")
        ),
        sa.CheckConstraint("purpose <> ''", name=op.f("ck_default_accounts_purpose_not_blank")),
        sa.CheckConstraint(
            "account_code <> ''", name=op.f("ck_default_accounts_account_code_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_default_accounts_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "package_id", "document_type", "purpose", name=op.f("pk_default_accounts")
        ),
    )


def _create_closing_account_pairs() -> None:
    op.create_table(
        "closing_account_pairs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("source_account", sa.String(length=20), nullable=False),
        sa.Column("target_account", sa.String(length=20), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "source_account <> ''", name=op.f("ck_closing_account_pairs_source_account_not_blank")
        ),
        sa.CheckConstraint(
            "target_account <> ''", name=op.f("ck_closing_account_pairs_target_account_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["config_packages.id"],
            name=op.f("fk_closing_account_pairs_package_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_closing_account_pairs")),
        sa.UniqueConstraint(
            "package_id",
            "source_account",
            "target_account",
            name="uq_closing_account_pairs_package_pair",
        ),
    )


def _apply_security() -> None:
    """Quyền cho vai trò runtime — không RLS (cấu hình dùng chung toàn dataset, xem `0009`)."""
    grantee = _dataset_grantee()
    for table, sequence in _GRANTS:
        for statement in grant_read_write(table, grantee=grantee, sequence=sequence):
            op.execute(statement)


def _refuse_downgrade_if_tt99_fiscal_years_exist() -> None:
    """Chặn hạ cấp khi dataset có năm tài chính TT99 — xem docstring đầu tệp.

    Chỉ đọc dữ liệu ở chế độ online (`context.is_offline_mode()` là `False`):
    chế độ `--sql` chỉ sinh văn bản DDL cho DBA duyệt trước khi chạy, không có
    connection thật để đọc dữ liệu — cùng ràng buộc đã ghi ở `_dataset_grantee`
    của `0001` (không hỏi DB bất cứ điều gì khi sinh SQL offline).
    """
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT count(*) FROM fiscal_years WHERE accounting_scheme = 'TT99'")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "Không hạ cấp được migration 0010: dataset này đã có "
            f"{count} năm tài chính dùng chế độ kế toán TT99 (bảng `fiscal_years`). "
            "Hạ cấp xuống dưới 0010 sẽ áp lại CHECK chỉ chấp nhận "
            "('TT200','TT133'), làm những năm đó vi phạm ràng buộc ngay lập tức. "
            "Xóa hoặc di chuyển các năm tài chính TT99 trước khi hạ cấp, nếu thật "
            "sự cần quay lại phiên bản schema cũ."
        )


def downgrade() -> None:
    _refuse_downgrade_if_tt99_fiscal_years_exist()
    op.drop_table("closing_account_pairs")
    op.drop_table("default_accounts")
    # Dọn dữ liệu TT99 trước khi áp lại CHECK cũ — xem docstring đầu tệp. Chỉ
    # còn `config_packages`/`chart_of_accounts` (dữ liệu gieo tự động) tới đây:
    # `fiscal_years` đã được gác cổng ở trên. `chart_of_accounts` trước (FK
    # `ondelete=RESTRICT` chặn xóa `config_packages` còn con trỏ vào), rồi mới
    # tới chính `config_packages`.
    op.execute(
        "DELETE FROM chart_of_accounts WHERE package_id IN "
        "(SELECT id FROM config_packages WHERE scheme = 'TT99')"
    )
    op.execute("DELETE FROM config_packages WHERE scheme = 'TT99'")
    op.drop_constraint("scheme_known", "config_packages", type_="check")
    op.create_check_constraint(
        "scheme_known", "config_packages", f"scheme IN ({_OLD_SCHEME_VALUES})"
    )
    op.drop_constraint("accounting_scheme_known", "fiscal_years", type_="check")
    op.create_check_constraint(
        "accounting_scheme_known", "fiscal_years", f"accounting_scheme IN ({_OLD_SCHEME_VALUES})"
    )
