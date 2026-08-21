"""Số dư đầu kỳ ngân hàng (kind 1) + ràng buộc khớp sao kê — lát 6D.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-21

Chạy **một lần cho mỗi schema dataset** như `0001`..`0018`, bằng `ket_owner`.

Ba việc:

1. `opening_balances.bank_account_id` — chiều TK ngân hàng cho nhóm 1 (sheet
   ngân hàng của số dư đầu kỳ, hoãn từ 4C vì danh mục `company_bank_accounts`
   chỉ có từ 6A). CHECK cặp `(detail_kind = 1) = (bank_account_id IS NOT NULL)`
   — dòng nhóm 1 không biết thuộc TK ngân hàng nào thì BR-BNK-01 không đối
   chiếu được.
2. Unique một phần trên `bank_statement_lines.matched_voucher_id`: một chứng
   từ khớp được đúng MỘT dòng sao kê — một khoản tiền vào tài khoản là một
   giao dịch ngân hàng, hai dòng cùng trỏ một chứng từ chỉ có thể là khớp nhầm,
   và để nó lọt thì báo cáo chênh lệch (FR-BNK-031) đếm thiếu.
3. `bank_statements.content_hash` — băm nội dung tệp đã nhập (kho đính kèm
   định địa chỉ theo nội dung đã giữ tệp; cột này để chống nhập ĐÚP cùng một
   tệp cho cùng một TK ngân hàng và để lần lại tệp gốc khi đối chiếu có nghi vấn).

Bước `_refresh_builtin_data` GIỮ ở 0017: không dataset builtin nào đọc các cột
mới (doctrine 6B chỉ đòi dời khi migration sau thêm cột mà dữ liệu builtin
tham chiếu).

`downgrade()` drop cột/index — chưa có bản cài phát hành.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTENT_HASH_LENGTH = 64


def upgrade() -> None:
    op.add_column(
        "opening_balances",
        sa.Column("bank_account_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_opening_balances_bank_account_id"),
        "opening_balances",
        "company_bank_accounts",
        ["bank_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_opening_balances_bank_account_iff_bank_kind"),
        "opening_balances",
        "(detail_kind = 1) = (bank_account_id IS NOT NULL)",
    )
    # Index phục vụ hai đường đọc thật: RESTRICT của FK khi xóa một TK ngân
    # hàng, và tổng dư đầu kỳ theo TK ngân hàng của đối chiếu/BFF thẻ tài khoản.
    op.create_index(
        op.f("ix_opening_balances_bank_account"),
        "opening_balances",
        ["bank_account_id"],
        postgresql_where=sa.text("bank_account_id IS NOT NULL"),
    )

    # Chiều tài khoản denormalize xuống dòng (bản sao bank_statements
    # .bank_account_id, dịch vụ nhập chép lúc ghi): unique một-chứng-từ-một-
    # dòng phải tính THEO TÀI KHOẢN — một chuyển nội bộ hợp lệ nằm trên HAI
    # sao kê (tiền ra ở nguồn, tiền vào ở đích). Bảng chưa có dữ liệu ở bản
    # cài nào nên thêm NOT NULL thẳng.
    op.add_column(
        "bank_statement_lines",
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_bank_statement_lines_bank_account_id"),
        "bank_statement_lines",
        "company_bank_accounts",
        ["bank_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_bank_statement_lines_matched_voucher",
        "bank_statement_lines",
        ["matched_voucher_id", "bank_account_id"],
        unique=True,
        postgresql_where=sa.text("matched_voucher_id IS NOT NULL"),
    )

    op.add_column(
        "bank_statements",
        sa.Column("content_hash", sa.String(length=_CONTENT_HASH_LENGTH), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bank_statements", "content_hash")
    op.drop_index("uq_bank_statement_lines_matched_voucher", table_name="bank_statement_lines")
    op.drop_constraint(
        op.f("fk_bank_statement_lines_bank_account_id"),
        "bank_statement_lines",
        type_="foreignkey",
    )
    op.drop_column("bank_statement_lines", "bank_account_id")
    op.drop_index(op.f("ix_opening_balances_bank_account"), table_name="opening_balances")
    op.drop_constraint(
        op.f("ck_opening_balances_bank_account_iff_bank_kind"),
        "opening_balances",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_opening_balances_bank_account_id"), "opening_balances", type_="foreignkey"
    )
    op.drop_column("opening_balances", "bank_account_id")
