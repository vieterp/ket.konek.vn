"""Khoản ứng trước thành dòng sổ phụ + nới trần loại đích của năm bảng đối trừ.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-06

Chạy **một lần cho mỗi schema dataset** như `0001`..`0029`, bằng `ket_owner`.

Không bảng mới. Lát này đóng hai điều kiện cuối chặn `arap_matches_control`
(quyết định user 2026-09-06), và cả hai đều là thay đổi trần chứ không thay
đổi hình dạng:

* `ar_ap_ledger.target_kind` nới `0..4` → `0..6` cho hai loại đích
  `ADVANCE_FROM_CUSTOMER`/`ADVANCE_TO_VENDOR`. Khoản ứng trước là một dòng sổ
  phụ **chiều ngược**: khách ứng trước tiền là nghĩa vụ của ta, nên nó mang
  chiều phải trả dù đối tác là khách hàng. Trước lát này hình dạng ấy không
  sinh dòng sổ phụ nào — sổ cái 131 nhích mà sổ phụ đứng yên, đúng điều kiện
  #2 mở từ 7A.
* Trần `target_kind` của **năm bảng đối trừ** nới theo thứ mỗi loại chứng từ
  tất toán được. Chứng từ tiền (`cash_settlements`, `bank_settlements`) và
  chứng từ nghiệp vụ khác (`gl_journal_settlements`) lên `0..6`; chứng từ giảm
  trừ mua/bán (`purchase_settlements`, `sales_settlements`) lên `0..4` — giảm
  giá hàng bán ghi giảm một khoản NỢ, nó không tất toán khoản khách đã ứng.

  Bốn bảng đầu trong số ấy còn dừng ở `0..2` trong khi `receivables` đã cấp
  `SettlementTargetSource` cho hai loại ghi tay từ 0029, nên hôm nay **thu tiền
  một khoản phải thu ghi tay nổ CHECK ở DB**: màn thu tiền liệt kê khoản ấy,
  server định giá xong, rồi lượt ghi mới hỏng. Nới trần sửa luôn lỗ ấy.

Cộng một bước dữ liệu: `partner_open_debt` (0026) **loại trừ** hai loại ứng
trước. Hàm trả các khoản còn nợ của một đối tác cho guard ngưỡng nợ, mà khoản
ứng trước là chiều ngược — cộng nó vào là thổi phồng nợ của mọi đối tác từng
ứng trước, và chặn bán hàng ở nửa hạn mức thật. Loại trừ giữ đúng hành vi guard
của hôm nay (hôm nay khoản ấy không tồn tại thành dòng nào để đếm); bù trừ hai
chiều là câu hỏi khác, có chủ đích để ngỏ.

Không đổi metadata builtin nào nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op

from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_TABLE = "ar_ap_ledger"

_SETTLEMENT_CEILINGS: tuple[tuple[str, int, int], ...] = (
    # (bảng, trần mới, trần cũ)
    #
    # `SettlementTargetKind`: 0 hóa đơn bán, 1 hóa đơn mua, 2 số dư đầu kỳ,
    # 3 phải thu ghi tay, 4 phải trả ghi tay, 5 khách ứng trước, 6 trả trước
    # người bán — số trần có chú thích, không import enum (migration là ảnh
    # chụp lịch sử, 0022 nêu cùng luật).
    ("cash_settlements", 6, 2),
    ("bank_settlements", 6, 2),
    ("purchase_settlements", 4, 2),
    ("sales_settlements", 4, 2),
    ("gl_journal_settlements", 6, 4),
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

    Tên ĐẦY ĐỦ và KHÔNG truyền `type_`: có `type_` thì Alembic áp quy ước đặt
    tên lên chuỗi đưa vào và dán thêm một tiền tố `ck_<bảng>_` nữa — lát 7C-3
    mất một vòng chạy (1118 test đỏ cùng lúc) vì đúng chỗ này. Lối đúng có sẵn
    từ `0002`.
    """
    op.drop_constraint(f"ck_{table}_target_kind_known", table)
    op.create_check_constraint("target_kind_known", table, f"target_kind BETWEEN 0 AND {ceiling}")


def _install_partner_open_debt_without_advances() -> None:
    """Dựng lại `partner_open_debt` (0026) LOẠI TRỪ hai loại ứng trước.

    Chép trọn thân hàm chứ không sửa tại chỗ: `CREATE OR REPLACE FUNCTION` đòi
    trọn định nghĩa. Hai bản (bản này và `_install_partner_open_debt_with_
    advances` ngay dưới) khác nhau **đúng một dòng** — dòng `NOT IN (5, 6)`; cả
    hai viết bằng chuỗi HẰNG thay vì một bản dựng theo cờ, vì cổng
    `test_no_sql_string_interpolation` cấm ghép chuỗi động vào SQL. Cổng ấy là
    hàng rào chính chống leo dataset (ADR-017), không phải luật hình thức.

    Khuôn và lý do từng mệnh đề ở `0024`/`0026`.
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
        # Nhánh dưới đã đếm chứng từ đầu kỳ; ngày lượt chuyển năm gộp chúng
        # thành dòng sổ phụ (`opening_invoice_id`, xem `arap_matches_control`)
        # thì không có vế này là đếm hai lần và chặn ở nửa hạn mức thật.
        "       AND opening_invoice_id IS NULL"
        # Khoản ứng trước là chiều NGƯỢC của khoản nợ: cộng nó vào là thổi
        # phồng nợ của mọi đối tác từng ứng trước, và guard ngưỡng nợ chặn ở
        # nửa hạn mức thật. Bù trừ hai chiều là câu hỏi khác, cố ý để ngỏ.
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
        # Nêu ĐỦ hai nhánh: loại đối tác khác (nhân viên) rơi vào NULL → không
        # dòng nào, thay vì lặng lẽ nhận nhóm "phải trả".
        "       AND o.detail_kind = CASE p_partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 END"
        "       AND i.amount_fc > i.paid_amount_fc"
        "       AND p_as_of BETWEEN y.start_date AND y.end_date"
        " $$"
    )


def _install_partner_open_debt_with_advances() -> None:
    """Bản 0026 nguyên trạng — chỉ dùng ở `downgrade`."""
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
        # Nhánh dưới đã đếm chứng từ đầu kỳ; ngày lượt chuyển năm gộp chúng
        # thành dòng sổ phụ (`opening_invoice_id`, xem `arap_matches_control`)
        # thì không có vế này là đếm hai lần và chặn ở nửa hạn mức thật.
        "       AND opening_invoice_id IS NULL"
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
        # Nêu ĐỦ hai nhánh: loại đối tác khác (nhân viên) rơi vào NULL → không
        # dòng nào, thay vì lặng lẽ nhận nhóm "phải trả".
        "       AND o.detail_kind = CASE p_partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 END"
        "       AND i.amount_fc > i.paid_amount_fc"
        "       AND p_as_of BETWEEN y.start_date AND y.end_date"
        " $$"
    )


def upgrade() -> None:
    op.drop_constraint(f"ck_{_LEDGER_TABLE}_target_kind_known", _LEDGER_TABLE)
    op.create_check_constraint("target_kind_known", _LEDGER_TABLE, "target_kind BETWEEN 0 AND 6")
    for table, ceiling, _previous in _SETTLEMENT_CEILINGS:
        _set_target_kind_ceiling(table, ceiling)
    _install_partner_open_debt_without_advances()


def downgrade() -> None:
    _install_partner_open_debt_with_advances()
    for table, _ceiling, previous in _SETTLEMENT_CEILINGS:
        _set_target_kind_ceiling(table, previous)
    op.drop_constraint(f"ck_{_LEDGER_TABLE}_target_kind_known", _LEDGER_TABLE)
    op.create_check_constraint("target_kind_known", _LEDGER_TABLE, "target_kind BETWEEN 0 AND 4")
