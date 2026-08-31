"""Chiều `bank_account` trên dòng sổ + chiều chi nhánh cho sao kê — lát 6G-1.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-31

Chạy **một lần cho mỗi schema dataset** như `0001`..`0021`, bằng `ket_owner`.

Hai thay đổi độc lập, một migration vì cả hai là nợ hạ tầng phải trả TRƯỚC khi
phase 7/8 nhân bản ra 12 module.

**1. `bank_account_id` — chiều thứ mười một của dòng hạch toán.** Cho tới lát
này, "dòng sổ 112x thuộc tài khoản ngân hàng nào" được SUY từ thân
`bank_vouchers` mỗi lần đọc, và luật suy ấy tồn tại năm bản: hai truy vấn
Python (`bank/balance_service`, `bank/reconciliation`) và ba câu SQL dataset.
Hai loại chứng từ không có thân để suy — phiếu quỹ nộp/rút tiền mặt ↔ ngân
hàng, và bút toán tổng hợp gõ thẳng 112x — nên rơi ra khỏi mọi báo cáo tiền
gửi; S08-DN lệch sao kê đúng bằng chúng (review 6E-1 H-3). Nay câu trả lời
được GHI một lần vào `gl_postings.bank_account_id` và năm bản đọc thu về một
phép so cột.

Backfill dựng lại đúng luật cũ cho dữ liệu đã ghi sổ (BC/UNC/SEC theo thân;
chuyển nội bộ thì bên Nợ thuộc tài khoản đích). Dòng 112x của phiếu quỹ và bút
toán tổng hợp **không** suy được — chúng ở lại NULL và tiếp tục hiện thành
nhóm "(chưa gắn)". Bịa một tài khoản cho chúng sẽ làm BR-BNK-01 "khớp" trên
một con số dối; kế toán sửa lại từng chứng từ là đường đúng.

Gói cấu hình bật `detail_tracking = bank_account` cho 112x (tt99 `112`, tt133
`1121`/`1122`), nên từ đây validator ghi sổ ĐÒI chiều này — chứng từ nháp cũ
chạm 112 phải bổ sung tài khoản ngân hàng trước khi ghi sổ lại. Version gói
bump 3→4 (tt99) và 4→5 (tt133) để dataset đang chạy nhận backfill (doctrine
5B M-1: dataset ở version cũ KHÔNG tự thấy dữ liệu gói mới).

**2. `branch_id` cho sao kê ngân hàng.** `bank_statements` và
`bank_statement_lines` sinh ra ở 0016/0019 hoàn toàn không có chiều chi nhánh,
nên `test_rls_policy_coverage` — vốn quét theo cột `branch_id` — không nhìn
thấy chúng và hai bảng đứng ngoài cô lập chi nhánh (review 6E-1 H-1). Chiều
lấy từ CHÍNH tài khoản ngân hàng (`company_bank_accounts.branch_id`, `NULL` =
dùng chung toàn công ty) chứ không phải chi nhánh của người nhập: sao kê là sổ
CỦA tài khoản, và hai nguồn sự thật cho cùng một câu hỏi là hai nguồn sẽ lệch.
Policy vì thế `allow_null_branch=True`, cùng khuôn `audit_log` của 0001.

Bước dữ liệu `_refresh_builtin_data` CHUYỂN TỪ 0021 sang đây: ba dataset báo
cáo giờ đọc cột `gl_postings.bank_account_id`, mà `refresh_builtin_reports`
probe SQL đóng gói HIỆN TẠI — để nó ở 0021 thì lượt nâng cấp nổ ngay tại 0021,
trước khi cột kịp tồn tại. Doctrine từ 6B, lần thứ tư áp dụng.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import refresh_builtin_reports
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.rls import enable_branch_rls_statements

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTERNAL_TRANSFER_KIND = 3
"""`BankVoucherKind.INTERNAL_TRANSFER` — literal vì migration là ảnh chụp lịch
sử: đổi tên hằng ở mã nguồn không được đổi nghĩa của một migration đã chạy."""


def upgrade() -> None:
    _add_bank_account_dimension()
    _backfill_bank_account_dimension()
    _add_statement_branch()
    _refresh_builtin_data()


def _add_bank_account_dimension() -> None:
    """Cột chiều trên dòng sổ + hai bảng dòng nháp điền nó.

    `bank_voucher_lines` cố ý KHÔNG có cột: chứng từ tiền gửi suy chủ sở hữu từ
    thân (luật CTNB, `bank/posting_mapper._deposit_owner`) nên một cột nhập tay
    ở đó là cửa thứ hai cho cùng một câu trả lời.

    Không FK tới `company_bank_accounts`, cùng lối với chín cột chiều đã có:
    `gl_postings` là sự thật đã ghi sổ, và một FK RESTRICT biến việc dọn danh
    mục thành việc sửa sổ.
    """
    for table in ("gl_postings", "cash_voucher_lines", "gl_journal_lines"):
        op.add_column(table, sa.Column("bank_account_id", sa.Integer(), nullable=True))

    # Sổ chi tiết tiền gửi và bảng kê số dư đều lọc "112x của một tài khoản"
    # rồi gộp — index một phần đúng hình dạng đó, không phủ dòng NULL (đại đa
    # số dòng sổ không chạm 112).
    op.create_index(
        op.f("ix_gl_postings_bank_account"),
        "gl_postings",
        ["bank_account_id"],
        postgresql_where=sa.text("bank_account_id IS NOT NULL"),
    )


def _backfill_bank_account_dimension() -> None:
    """Dựng lại luật quy chủ CŨ cho dòng sổ đã ghi của chứng từ tiền gửi.

    Chỉ `bank_vouchers`: đó là tập duy nhất mà luật cũ suy được, và cũng đúng
    bằng tập mà báo cáo trước lát này nhìn thấy — nên số liệu không đổi ở đâu
    cả, chỉ chuyển từ tính-lúc-đọc sang lưu-lúc-ghi.

    Lọc `112%` theo số hiệu TK chứ không theo `bank_voucher_lines`: một chứng
    từ tiền gửi có cả dòng phí (642) lẫn dòng thuế, và chỉ bên 112x mới thuộc
    về một tài khoản ngân hàng.
    """
    op.execute(
        sa.text(
            """
            UPDATE gl_postings p
               SET bank_account_id = CASE
                       WHEN bv.kind = :transfer_kind AND p.debit > 0
                           THEN bv.counter_bank_account_id
                       ELSE bv.bank_account_id
                   END
              FROM bank_vouchers bv, chart_of_accounts coa
             WHERE bv.id = p.voucher_id
               AND coa.id = p.account_id
               AND coa.code LIKE '112%'
            """
        ).bindparams(transfer_kind=_INTERNAL_TRANSFER_KIND)
    )


def _add_statement_branch() -> None:
    """Chiều chi nhánh + RLS cho sao kê, lấy từ tài khoản ngân hàng.

    Denormalize xuống dòng cùng lý do `bank_account_id` của 0019: RLS lọc theo
    cột CỦA CHÍNH bảng, và một policy phải join lên bảng cha là policy chạy
    trên mọi dòng của mọi truy vấn.
    """
    backfills = (
        (
            "bank_statements",
            """
            UPDATE bank_statements t
               SET branch_id = cba.branch_id
              FROM company_bank_accounts cba
             WHERE cba.id = t.bank_account_id
            """,
        ),
        (
            "bank_statement_lines",
            """
            UPDATE bank_statement_lines t
               SET branch_id = cba.branch_id
              FROM company_bank_accounts cba
             WHERE cba.id = t.bank_account_id
            """,
        ),
    )
    for table, backfill in backfills:
        op.add_column(table, sa.Column("branch_id", sa.Integer(), nullable=True))
        op.execute(sa.text(backfill))
        # `allow_null_branch=True`: TK ngân hàng dùng chung toàn công ty
        # (`branch_id IS NULL`) thì sao kê của nó phải nhìn thấy được từ mọi
        # chi nhánh — cùng đối xử với sự kiện mức hệ thống ở `audit_log`.
        for statement in enable_branch_rls_statements(table, allow_null_branch=True):
            op.execute(statement)


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — CHUYỂN TỪ 0021 (xem docstring
    đầu tệp). Chỉ chạy online, cùng lý do với bản gốc: bước dữ liệu đọc-rồi-ghi
    không diễn đạt được thành SQL tĩnh của `upgrade --sql`."""
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def downgrade() -> None:
    teardown = (
        (
            "bank_statement_lines",
            'DROP POLICY IF EXISTS "p_branch_scope" ON bank_statement_lines',
            "ALTER TABLE bank_statement_lines DISABLE ROW LEVEL SECURITY",
        ),
        (
            "bank_statements",
            'DROP POLICY IF EXISTS "p_branch_scope" ON bank_statements',
            "ALTER TABLE bank_statements DISABLE ROW LEVEL SECURITY",
        ),
    )
    for table, drop_policy, disable_rls in teardown:
        op.execute(drop_policy)
        op.execute(disable_rls)
        op.drop_column(table, "branch_id")
    op.drop_index(op.f("ix_gl_postings_bank_account"), table_name="gl_postings")
    for table in ("gl_journal_lines", "cash_voucher_lines", "gl_postings"):
        op.drop_column(table, "bank_account_id")
