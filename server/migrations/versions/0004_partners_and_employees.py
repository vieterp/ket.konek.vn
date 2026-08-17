"""Đối tác (kèm tài khoản ngân hàng) và nhân viên.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17

Chạy **một lần cho mỗi schema dataset** như `0001`..`0003`, bằng `ket_owner`.

Ba bảng:

1. `partners` — một danh mục cho cả khách hàng lẫn nhà cung cấp (FR-SYS-031),
   mang thêm mười bốn cột riêng (định danh, địa chỉ, liên hệ, hóa đơn, công nợ).
2. `partner_bank_accounts` — bảng con, nhiều tài khoản cho mỗi đối tác
   (FR-SYS-033). **Không** phải danh mục nên không dùng bộ cột chung.
3. `employees` — nhân viên (FR-SYS-035/036), phần định danh và tài khoản nhận
   lương; tham số tính lương/thuế TNCN thuộc phase 9 (H59).

`_create_master_data_table` lặp lại y như `0003` thay vì import: migration là ảnh
chụp lịch sử, và một hàm dùng chung giữa các bản sẽ đổi hình dạng của bản cũ mỗi
lần bản mới cần thêm gì — đúng kiểu thay đổi làm hai bản cài cùng số revision mà
khác schema.

**Không** bật RLS cho bảng nào, cùng lập luận `0002`/`0003`: danh mục dùng chung
mang `branch_id IS NULL` (FR-SYS-018) và policy chi nhánh sẽ giấu đúng những dòng
đó. `partner_bank_accounts` không có `branch_id` — phạm vi của nó là phạm vi của
đối tác chủ. Miễn trừ khai tường minh trong `tests/test_rls_policy_coverage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.security.grants import grant_read_write, serial_sequence_name

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = 50
_NAME = 255
_PATH = 255
_TAX_CODE = 20
_ADDRESS = 500
_PHONE = 50
_EMAIL = 255
_WEBSITE = 255
_REGION = 100
_ID_NUMBER = 20
_BANK_ACCOUNT = 50
_BANK_BRANCH = 255
_DEPARTMENT = 150
_POSITION = 150
_CREDIT_LIMIT_PRECISION = 20
_CREDIT_LIMIT_SCALE = 6

_PARTNER_BANK_ACCOUNTS = "partner_bank_accounts"
_CATALOGS = ("partners", "employees")


def upgrade() -> None:
    _create_partners()
    _create_employees()
    _create_partner_bank_accounts()
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


def _create_partners() -> None:
    _create_master_data_table(
        "partners",
        sa.Column("is_customer", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_vendor", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_organization", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("tax_code", sa.String(length=_TAX_CODE), nullable=True),
        sa.Column("address", sa.String(length=_ADDRESS), nullable=True),
        sa.Column("country", sa.String(length=_REGION), nullable=True),
        sa.Column("province", sa.String(length=_REGION), nullable=True),
        sa.Column("district", sa.String(length=_REGION), nullable=True),
        sa.Column("contact_name", sa.String(length=_NAME), nullable=True),
        sa.Column("phone", sa.String(length=_PHONE), nullable=True),
        sa.Column("email", sa.String(length=_EMAIL), nullable=True),
        sa.Column("website", sa.String(length=_WEBSITE), nullable=True),
        sa.Column("invoice_recipient", sa.String(length=_NAME), nullable=True),
        sa.Column("invoice_email", sa.String(length=_EMAIL), nullable=True),
        sa.Column("payment_term_id", sa.Integer(), nullable=True),
        sa.Column(
            "credit_limit",
            sa.Numeric(precision=_CREDIT_LIMIT_PRECISION, scale=_CREDIT_LIMIT_SCALE),
            nullable=True,
        ),
        sa.CheckConstraint(
            "is_group OR is_customer OR is_vendor",
            name=op.f("ck_partners_partner_is_customer_or_vendor"),
        ),
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name=op.f("ck_partners_credit_limit_not_negative"),
        ),
        sa.CheckConstraint(
            "tax_code IS NULL OR tax_code <> ''", name=op.f("ck_partners_tax_code_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["payment_term_id"],
            ["payment_terms.id"],
            name=op.f("fk_partners_payment_term_id"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_partners_tax_code", "partners", ["tax_code"])
    op.create_index("ix_partners_payment_term_id", "partners", ["payment_term_id"])


def _create_employees() -> None:
    _create_master_data_table(
        "employees",
        sa.Column("department", sa.String(length=_DEPARTMENT), nullable=True),
        sa.Column("position", sa.String(length=_POSITION), nullable=True),
        sa.Column("id_number", sa.String(length=_ID_NUMBER), nullable=True),
        sa.Column("tax_code", sa.String(length=_TAX_CODE), nullable=True),
        sa.Column("phone", sa.String(length=_PHONE), nullable=True),
        sa.Column("email", sa.String(length=_EMAIL), nullable=True),
        sa.Column("bank_id", sa.Integer(), nullable=True),
        sa.Column("bank_account_number", sa.String(length=_BANK_ACCOUNT), nullable=True),
        sa.Column("bank_account_holder", sa.String(length=_NAME), nullable=True),
        sa.CheckConstraint(
            "id_number IS NULL OR id_number <> ''", name=op.f("ck_employees_id_number_not_blank")
        ),
        sa.CheckConstraint(
            "tax_code IS NULL OR tax_code <> ''", name=op.f("ck_employees_tax_code_not_blank")
        ),
        sa.CheckConstraint(
            "(bank_account_number IS NULL) = (bank_id IS NULL)",
            name=op.f("ck_employees_bank_account_needs_bank"),
        ),
        sa.ForeignKeyConstraint(
            ["bank_id"], ["banks.id"], name=op.f("fk_employees_bank_id"), ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_employees_tax_code", "employees", ["tax_code"])
    op.create_index("ix_employees_bank_id", "employees", ["bank_id"])


def _create_partner_bank_accounts() -> None:
    op.create_table(
        _PARTNER_BANK_ACCOUNTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("bank_id", sa.Integer(), nullable=False),
        sa.Column("account_number", sa.String(length=_BANK_ACCOUNT), nullable=False),
        sa.Column("account_holder", sa.String(length=_NAME), nullable=False),
        sa.Column("bank_branch", sa.String(length=_BANK_BRANCH), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "account_number <> ''",
            name=op.f(f"ck_{_PARTNER_BANK_ACCOUNTS}_account_number_not_blank"),
        ),
        sa.CheckConstraint(
            "account_holder <> ''",
            name=op.f(f"ck_{_PARTNER_BANK_ACCOUNTS}_account_holder_not_blank"),
        ),
        # `CASCADE` theo đối tác: tài khoản thuộc về đối tác, không tồn tại độc
        # lập. `RESTRICT` theo ngân hàng: ngân hàng là danh mục dùng chung, xóa
        # nó khi còn tài khoản trỏ tới là mất tên ngân hàng trên ủy nhiệm chi.
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["partners.id"],
            name=op.f(f"fk_{_PARTNER_BANK_ACCOUNTS}_partner_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bank_id"],
            ["banks.id"],
            name=op.f(f"fk_{_PARTNER_BANK_ACCOUNTS}_bank_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_PARTNER_BANK_ACCOUNTS}")),
        sa.UniqueConstraint(
            "partner_id",
            "account_number",
            name=f"uq_{_PARTNER_BANK_ACCOUNTS}_partner_account",
        ),
    )
    # Đúng **một** tài khoản mặc định cho mỗi đối tác — chỉ mục có điều kiện là
    # chỗ duy nhất nối tiếp được hai request cùng đặt mặc định.
    op.create_index(
        f"uq_{_PARTNER_BANK_ACCOUNTS}_default",
        _PARTNER_BANK_ACCOUNTS,
        ["partner_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        f"ix_{_PARTNER_BANK_ACCOUNTS}_partner_id", _PARTNER_BANK_ACCOUNTS, ["partner_id"]
    )
    op.create_index(f"ix_{_PARTNER_BANK_ACCOUNTS}_bank_id", _PARTNER_BANK_ACCOUNTS, ["bank_id"])


def _apply_grants() -> None:
    """Quyền cho vai trò runtime của schema đang migrate (D3, RT-02).

    Cả ba bảng có `SERIAL` id nên phải cấp thêm quyền trên **sequence**: thiếu nó
    thì `INSERT` đầu tiên đổ với `permission denied for sequence`, ở đúng thao
    tác đầu tiên của người dùng chứ không ở CI.
    """
    grantee = _dataset_grantee()
    for table in (*_CATALOGS, _PARTNER_BANK_ACCOUNTS):
        for statement in grant_read_write(
            table, grantee=grantee, sequence=serial_sequence_name(table)
        ):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table(_PARTNER_BANK_ACCOUNTS)
    op.drop_table("employees")
    op.drop_table("partners")
