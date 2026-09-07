"""Khoản ứng trước đầu kỳ thành dòng chi tiết + nới trần loại đích lên 7.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-06

Chạy **một lần cho mỗi schema dataset** như `0001`..`0030`, bằng `ket_owner`.

Không bảng mới. Lát này đóng ba điều kiện cuối chặn `arap_matches_control`, và
hai trong ba nằm ở đây:

* `opening_balance_invoices.is_advance` — TK lưỡng tính được phép vừa còn nợ
  vừa có khoản khách ứng trước trên **cùng một dòng cha** (BR-OPB-03), nhưng
  trước lát này bên ứng trước chỉ vào cột `credit` của dòng cha mà không để
  lại dòng con nào: vế sổ cái nhích, vế sổ phụ đứng yên. Cột boolean đưa khoản
  ấy vào đúng bảng mà hóa đơn đầu kỳ đã ở, nên nó đi chung mọi đường đã có —
  phạm vi năm, lượt chuyển năm, nguồn đối trừ.

* **Bước dữ liệu**: mỗi dòng cha nhóm 2/3/4 có bên NGƯỢC khác 0 sinh đúng một
  dòng con ứng trước. Không backfill thì dữ liệu đã nhập trước lát này vẫn làm
  check đỏ, và một check kêu sai trên dữ liệu cũ cũng là một check người ta
  học cách bỏ qua.

* Trần `target_kind` của ba bảng đối trừ tất toán được khoản ứng trước đầu kỳ
  (`cash_settlements`, `bank_settlements`, `gl_journal_settlements`) nới
  `0..6` → `0..7`. Hai bảng giảm trừ mua/bán giữ `0..4` như 0030 đã chọn.
  `ar_ap_ledger` **không đổi**: khoản ứng trước đầu kỳ sống ở bảng đầu kỳ, nó
  không phải một dòng sổ phụ chứng từ.

Cộng một bước dữ liệu thứ hai: `partner_open_debt` (0026, sửa ở 0030) loại trừ
dòng con ứng trước. Nhánh đầu kỳ của hàm ấy cộng MỌI dòng con thành nợ; từ lát
này bảng có cả chiều ngược, nên thiếu vế lọc là guard ngưỡng nợ đếm tiền khách
ứng trước thành tiền khách nợ — đúng loại lỗi mà `target_kind NOT IN (5, 6)`
của 0030 đã chặn ở nhánh trên.

Không đổi metadata builtin nào nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVOICE_TABLE = "opening_balance_invoices"

_SETTLEMENT_CEILINGS: tuple[tuple[str, int, int], ...] = (
    # (bảng, trần mới, trần cũ) — `SettlementTargetKind.OPENING_ADVANCE = 7`.
    # Số trần có chú thích, không import enum (migration là ảnh chụp lịch sử,
    # 0022 nêu cùng luật).
    ("cash_settlements", 7, 6),
    ("bank_settlements", 7, 6),
    ("gl_journal_settlements", 7, 6),
)


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _set_target_kind_ceiling(table: str, ceiling: int) -> None:
    """Thay ràng buộc trần loại đích của một bảng.

    Tên ĐẦY ĐỦ và KHÔNG truyền `type_` — lý do ở `0030`, cùng chỗ đã mất một
    vòng chạy ở 7C-3.
    """
    op.drop_constraint(f"ck_{table}_target_kind_known", table)
    op.create_check_constraint("target_kind_known", table, f"target_kind BETWEEN 0 AND {ceiling}")


def _backfill_advances() -> None:
    """Sinh dòng con ứng trước cho mọi dòng cha đã nhập có bên ngược khác 0.

    Bên NGƯỢC theo nhóm: nhóm phải thu (2) và tạm ứng nhân viên (4) có bên
    còn-nợ là Nợ nên bên ngược là Có; nhóm phải trả (3) ngược lại. Cùng bảng
    `NATURAL_DEBIT_KINDS` của `parsing.py`, viết thành hai nhánh `CASE` vì
    migration không import mã ứng dụng.

    `gen_random_uuid()` chứ không `uuid7()`: khóa chính của bảng này là UUID
    sinh ở tầng Python (ADR-012), nhưng một lượt backfill không có thứ tự thời
    gian để bảo tồn — thứ nó cần là duy nhất, và hàm ấy có sẵn trong PostgreSQL
    từ bản 13 (`kernel/excel/models.py` nêu cùng giới hạn ở chiều ngược lại).
    """
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "INSERT INTO opening_balance_invoices ("
        "    id, opening_balance_id, branch_id, invoice_no, invoice_date, due_date,"
        "    amount_fc, amount, paid_amount, paid_amount_fc, is_advance"
        ") SELECT gen_random_uuid(), ob.id, ob.branch_id, NULL, NULL, NULL,"
        "         CASE WHEN ob.detail_kind = 3 THEN ob.debit_fc ELSE ob.credit_fc END,"
        "         CASE WHEN ob.detail_kind = 3 THEN ob.debit ELSE ob.credit END,"
        "         0, 0, TRUE"
        "    FROM opening_balances ob"
        "   WHERE ob.detail_kind IN (2, 3, 4)"
        "     AND CASE WHEN ob.detail_kind = 3 THEN ob.debit ELSE ob.credit END > 0"
    )


def _install_partner_open_debt_without_opening_advances() -> None:
    """Dựng lại `partner_open_debt` (0026, sửa ở 0030) BỎ dòng con ứng trước.

    Chép trọn thân hàm chứ không sửa tại chỗ (`CREATE OR REPLACE FUNCTION` đòi
    trọn định nghĩa), và hai bản (bản này với bản `_with_` ngay dưới) khác nhau
    **đúng một dòng** — dòng `i.is_advance = FALSE`. Cả hai viết bằng chuỗi
    HẰNG thay vì một bản dựng theo cờ, vì cổng `test_no_sql_string_interpolation`
    cấm ghép chuỗi động vào SQL: hàng rào chính chống leo dataset (ADR-017),
    không phải luật hình thức. Khuôn và lý do từng mệnh đề ở `0024`/`0026`/`0030`.
    """
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION partner_open_debt("
        "p_partner_kind smallint, p_partner_id integer, p_as_of date)"
        " RETURNS TABLE("
        "document_no text, document_date date, due_date date, remaining numeric)"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT document_no::text, document_date, due_date, amount - settled"
        "     FROM ar_ap_ledger"
        "     WHERE partner_kind = p_partner_kind"
        "       AND partner_id = p_partner_id"
        "       AND ledger = 0"
        "       AND is_closed = FALSE"
        "       AND opening_invoice_id IS NULL"
        "       AND target_kind NOT IN (5, 6)"
        "     UNION ALL"
        "     SELECT COALESCE(i.invoice_no, '')::text,"
        "            COALESCE(i.invoice_date, y.start_date),"
        "            i.due_date,"
        "            i.amount - i.paid_amount"
        "     FROM opening_balance_invoices i"
        "     JOIN opening_balances o ON o.id = i.opening_balance_id"
        "     JOIN fiscal_years y ON y.id = o.fiscal_year_id"
        "     WHERE o.partner_kind = p_partner_kind"
        "       AND o.partner_id = p_partner_id"
        "       AND o.ledger = 0"
        "       AND o.detail_kind = CASE p_partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 END"
        "       AND i.amount_fc > i.paid_amount_fc"
        "       AND p_as_of BETWEEN y.start_date AND y.end_date"
        # Dòng con ứng trước là chiều NGƯỢC của khoản nợ, cùng lẽ với
        # `target_kind NOT IN (5, 6)` ở nhánh trên: cộng nó vào là đếm tiền
        # khách đã ứng thành tiền khách nợ và chặn bán hàng ở nửa hạn mức thật.
        "       AND i.is_advance = FALSE"
        " $$"
    )


def _install_partner_open_debt_with_opening_advances() -> None:
    """Bản 0030 nguyên trạng — chỉ dùng ở `downgrade`, khi cột đã biến mất."""
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION partner_open_debt("
        "p_partner_kind smallint, p_partner_id integer, p_as_of date)"
        " RETURNS TABLE("
        "document_no text, document_date date, due_date date, remaining numeric)"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT document_no::text, document_date, due_date, amount - settled"
        "     FROM ar_ap_ledger"
        "     WHERE partner_kind = p_partner_kind"
        "       AND partner_id = p_partner_id"
        "       AND ledger = 0"
        "       AND is_closed = FALSE"
        "       AND opening_invoice_id IS NULL"
        "       AND target_kind NOT IN (5, 6)"
        "     UNION ALL"
        "     SELECT COALESCE(i.invoice_no, '')::text,"
        "            COALESCE(i.invoice_date, y.start_date),"
        "            i.due_date,"
        "            i.amount - i.paid_amount"
        "     FROM opening_balance_invoices i"
        "     JOIN opening_balances o ON o.id = i.opening_balance_id"
        "     JOIN fiscal_years y ON y.id = o.fiscal_year_id"
        "     WHERE o.partner_kind = p_partner_kind"
        "       AND o.partner_id = p_partner_id"
        "       AND o.ledger = 0"
        "       AND o.detail_kind = CASE p_partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 END"
        "       AND i.amount_fc > i.paid_amount_fc"
        "       AND p_as_of BETWEEN y.start_date AND y.end_date"
        " $$"
    )


def upgrade() -> None:
    op.add_column(
        _INVOICE_TABLE,
        sa.Column("is_advance", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_check_constraint(
        "advance_has_no_invoice_ref",
        _INVOICE_TABLE,
        "NOT is_advance OR (invoice_no IS NULL AND invoice_date IS NULL)",
    )
    _backfill_advances()
    for table, ceiling, _previous in _SETTLEMENT_CEILINGS:
        _set_target_kind_ceiling(table, ceiling)
    _install_partner_open_debt_without_opening_advances()


def downgrade() -> None:
    _install_partner_open_debt_with_opening_advances()
    # Dữ liệu TRƯỚC ràng buộc, không phải sau: `CREATE ... CHECK` của PostgreSQL
    # kiểm dòng hiện có ngay lúc tạo, nên hạ trần trong khi còn dòng đối trừ
    # loại 7 sẽ nổ giữa chừng lượt hạ cấp.
    #
    # Dòng ứng trước biến mất cùng cột: giữ lại chúng sau khi bỏ cột là để một
    # khoản chiều ngược đội lốt khoản nợ. Lượt `upgrade` sau đó dựng lại được
    # SỐ TIỀN từ chính dòng cha nhưng **không** dựng lại được `paid_amount` —
    # hạ cấp sau khi đã tất toán khoản ứng trước là mất phần đã tất toán ấy, và
    # đó là lý do hạ cấp một dataset đang chạy vẫn phải đi kèm khôi phục sao
    # lưu (`docs/deployment-guide` §3.2).
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "DELETE FROM cash_settlements WHERE target_kind = 7;"
        "DELETE FROM bank_settlements WHERE target_kind = 7;"
        "DELETE FROM gl_journal_settlements WHERE target_kind = 7;"
        "DELETE FROM opening_balance_invoices WHERE is_advance;"
    )
    for table, _ceiling, previous in _SETTLEMENT_CEILINGS:
        _set_target_kind_ceiling(table, previous)
    op.drop_constraint(f"ck_{_INVOICE_TABLE}_advance_has_no_invoice_ref", _INVOICE_TABLE)
    op.drop_column(_INVOICE_TABLE, "is_advance")
