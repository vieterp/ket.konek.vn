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
chạm 112 phải bổ sung tài khoản ngân hàng trước khi ghi sổ lại.

**Dataset ĐÃ CẤP nhận chiều ấy bằng câu `UPDATE` dưới đây, không bằng bump
version gói.** Bump version là đường SAI ở đây, và sai theo hai hướng: `_seed_one`
gặp version lệch thì `return` TRƯỚC mọi backfill (không có câu `UPDATE
chart_of_accounts` nào trên đường gieo mầm — nó chỉ chèn TK cho gói MỚI), nên
chiều không bao giờ tới nơi; và cùng lượt `return` ấy tắt luôn
`_ensure_statements_backfilled`/`_ensure_auto_posting_backfilled` cho mọi dataset
cũ — một hồi quy nằm ngoài phạm vi lát này. Doctrine 5B M-1 nói dataset ở version
cũ KHÔNG tự thấy dữ liệu gói mới; nó là lý do phải viết câu `UPDATE`, không phải
lý do để bump.

**2. `branch_id` cho sao kê ngân hàng.** `bank_statements` và
`bank_statement_lines` sinh ra ở 0016/0019 hoàn toàn không có chiều chi nhánh,
nên `test_rls_policy_coverage` — vốn quét theo cột `branch_id` — không nhìn
thấy chúng và hai bảng đứng ngoài cô lập chi nhánh (review 6E-1 H-1). Chiều
lấy từ CHÍNH tài khoản ngân hàng (`company_bank_accounts.branch_id`, `NULL` =
dùng chung toàn công ty) chứ không phải chi nhánh của người nhập: sao kê là sổ
CỦA tài khoản, và hai nguồn sự thật cho cùng một câu hỏi là hai nguồn sẽ lệch.
Policy vì thế `allow_null_branch=True`, cùng khuôn `audit_log` của 0001.

Bước dữ liệu `_refresh_builtin_data` từng ở đây (chuyển 0021 → 0022 ở lát
6G-1) đã CHUYỂN TIẾP sang `0023`, đúng doctrine "bước làm mới đứng ở CUỐI
chuỗi": lát 6G-2 thêm cột `report_definitions.requires_full_branch_scope` mà
chính dữ liệu builtin ghi vào — để bước dữ liệu ở lại đây thì lượt nâng cấp nổ
tại 0022, trước khi cột kịp tồn tại. Ghi chú gốc giữ nguyên vì lý do của nó vẫn
đúng: ba dataset báo cáo đọc cột `gl_postings.bank_account_id`, mà
`refresh_builtin_reports`
probe SQL đóng gói HIỆN TẠI — để nó ở 0021 thì lượt nâng cấp nổ ngay tại 0021,
trước khi cột kịp tồn tại. Doctrine từ 6B, lần thứ tư áp dụng.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

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
    _enable_bank_account_tracking()
    _add_statement_branch()


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


def _enable_bank_account_tracking() -> None:
    """Bật `bank_account` trên 112x của hai gói dựng sẵn — cho dataset ĐÃ CẤP.

    Dataset cấp MỚI đọc thẳng `accounts.csv` nên đã có chiều; dataset cũ thì
    không, và đường gieo mầm không có chỗ nào sửa `chart_of_accounts` của một
    gói đã tồn tại (xem docstring đầu tệp). Thiếu câu này thì lát 6G-1 chỉ có
    tác dụng trên bản cài mới: TK 112x không đòi chiều, phiếu quỹ và bút toán
    tổng hợp chạm 112 tiếp tục để trống, mà đường ĐỌC thì đã bỏ hết luật suy cũ
    — không còn cơ chế bù nào.

    Chỉ hai gói dựng sẵn: gói người dùng tự nhập chịu trách nhiệm bằng chính
    `accounts.csv` của nó, và loader có phép kiểm riêng bắt thiếu chiều này.
    Không đụng TK tổng hợp (không ai hạch toán thẳng vào).

    Idempotent: `NOT (detail_tracking @> ARRAY['bank_account'])` bỏ qua dòng đã
    có, nên chạy lại không nhân đôi phần tử.
    """
    op.execute(
        sa.text(
            """
            UPDATE chart_of_accounts
               SET detail_tracking =
                       coalesce(detail_tracking, ARRAY[]::varchar[])
                       || ARRAY['bank_account']::varchar[]
              FROM config_packages p
             WHERE p.id = chart_of_accounts.package_id
               AND p.code IN ('TT99-2025', 'TT133-2016')
               AND chart_of_accounts.code LIKE '112%'
               AND chart_of_accounts.is_summary = false
               AND NOT (
                       coalesce(detail_tracking, ARRAY[]::varchar[])
                       @> ARRAY['bank_account']::varchar[]
                   )
            """
        )
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
