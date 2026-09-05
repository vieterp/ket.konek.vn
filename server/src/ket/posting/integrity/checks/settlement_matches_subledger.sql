-- BR-QUY-02 ở mặt SỐ ĐÃ TRẢ: số đối trừ ghi trên sổ phụ phải bằng đúng tổng
-- các dòng đối trừ của những chứng từ ĐANG ghi sổ trỏ vào nó — chứng từ tiền
-- (phiếu thu/chi, báo có/nợ) và chứng từ trả lại hàng mua (lát 7B).
--
-- Đây là phép kiểm khép kín — nó không hỏi sổ cái câu nào, nên không dính bốn
-- nguồn báo-sai đã chặn `arap_matches_control.sql` (xem đầu tệp ấy). Thứ nó
-- bắt là **lệch giữa đường cộng và đường gỡ**: `apply_settlement_rows` cộng
-- lúc ghi sổ, `revert_settlement_rows` gỡ lúc bỏ ghi sổ, và hai đường ấy chỉ
-- đúng chừng nào chúng đi cùng nhau. Ba cách chúng lệch trong đời thật:
-- chứng từ tiền bị xóa thẳng bằng SQL (dòng đối trừ đi theo CASCADE, số đã
-- cộng ở lại), một lượt bỏ ghi sổ gỡ thiếu, hoặc một đường ghi tương lai cộng
-- vào `settled` mà không để lại dòng đối trừ nào.
--
-- **Số VND so là `amount − fx_diff`, không phải `amount`.** `apply` nhận
-- `amount = row.amount − row.fx_diff` (`posting/settlements.py`): `amount` là
-- VND theo tỷ giá CHỨNG TỪ, còn thứ được giải phóng trên sổ là VND theo tỷ giá
-- GHI NHẬN NỢ, và phần chênh đi vào 515/635. So bằng `amount` thì mọi lượt đối
-- trừ ngoại tệ và mọi lát gánh phần lẻ làm tròn đều bị báo sai — nghĩa là
-- check đỏ đúng ở những chứng từ khó nhất, nơi người ta cần nó im nhất.
--
-- **Hai sổ phụ, tách bằng `target_kind`** (`kernel.protocols.
-- SettlementTargetKind`): 0/1 (hóa đơn bán/mua) và 3/4 (khoản phải thu/phải
-- trả ghi thẳng bằng chứng từ nghiệp vụ khác, lát 7C-3) là `ar_ap_ledger`,
-- 2 (số dư đầu kỳ) là `opening_balance_invoices`. Dòng `ar_ap_ledger` mang
-- `target_kind = 2` KHÔNG được nối vào đây: `target_id` của một dòng đối trừ
-- loại 2 là `opening_balance_invoices.id`, không phải id của sổ phụ — nối
-- nhầm thì hai bảng khác nhau tranh cùng một khóa.
--
-- **Chỉ chứng từ `status = 2`** (`VoucherStatus.DA_GHI_SO`): chứng từ mới Cất
-- đã có dòng đối trừ nhưng chưa cộng vào sổ phụ, và một chứng từ vừa bỏ ghi sổ
-- giữ nguyên dòng đối trừ của nó — đếm cả hai loại là tự dựng ra chênh lệch.
--
-- **Lọc chi nhánh ở CẢ HAI vế.** `price_settlements` bắt chứng từ và đích cùng
-- chi nhánh (`settlement.branch_mismatch`), nên một dòng đối trừ luôn cùng chi
-- nhánh với đích của nó — kể cả khi đích đã bị xóa, dòng mồ côi vẫn nằm ở
-- đúng chi nhánh mà đích từng ở. Lọc một vế thôi thì mỗi chi nhánh lại báo
-- dòng mồ côi của chi nhánh khác.
--
-- FULL JOIN chứ không INNER: chiều "sổ phụ có số, không có dòng đối trừ nào"
-- và chiều "dòng đối trừ trỏ vào một đích không còn tồn tại" đều là lệch.
-- Chiều thứ hai hiện **đã được một guard đóng**: `ensure_groups_not_settled`
-- (`opening_balances/service.py`) chặn nhập-lại/xóa nhóm số dư còn chứng từ
-- đã đối trừ, và `ensure_not_settled` (`receivables/ledger_service.py`, đăng
-- ký vào `REFERENCE_GUARDS`) chặn bỏ ghi sổ/xóa chứng từ gốc còn khoản đã
-- trả. Vế FULL JOIN vì thế là lưới đỡ cho ngày một trong hai guard bị gỡ hoặc
-- bị đi vòng bằng SQL trực tiếp, không phải cho một lối đi đang mở.
WITH settlement_rows AS (
    SELECT s.target_kind,
           s.target_id,
           s.amount_fc                 AS amount_fc,
           s.amount - s.fx_diff        AS recognised,
           v.branch_id                 AS branch_id,
           v.voucher_no                AS voucher_no
    FROM cash_settlements s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE v.status = 2
    UNION ALL
    SELECT s.target_kind,
           s.target_id,
           s.amount_fc,
           s.amount - s.fx_diff,
           v.branch_id,
           v.voucher_no
    FROM bank_settlements s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE v.status = 2
    UNION ALL
    -- Chứng từ trả lại hàng mua đối trừ vào hóa đơn gốc bằng cùng cơ chế với
    -- phiếu chi (`purchase/settlement_service.py`), nên bảng đối trừ của nó
    -- cũng là một nguồn cộng vào `settled` — thiếu nhánh này thì mỗi lượt trả
    -- hàng là một dòng đỏ "sổ phụ có số, không có dòng đối trừ nào".
    SELECT s.target_kind,
           s.target_id,
           s.amount_fc,
           s.amount - s.fx_diff,
           v.branch_id,
           v.voucher_no
    FROM purchase_settlements s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE v.status = 2
    UNION ALL
    -- Chứng từ trả lại hàng bán và giảm giá hàng bán (lát 7C-2) đối trừ hóa
    -- đơn gốc bằng đúng cơ chế ấy, đổi chiều — nguồn thứ tư cộng vào `settled`.
    SELECT s.target_kind,
           s.target_id,
           s.amount_fc,
           s.amount - s.fx_diff,
           v.branch_id,
           v.voucher_no
    FROM sales_settlements s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE v.status = 2
    UNION ALL
    -- Chứng từ nghiệp vụ khác (lát 7C-3): dòng ghi GIẢM một khoản công nợ đi
    -- qua cùng cơ chế đối trừ, nên đây là nguồn thứ năm cộng vào `settled`.
    -- Bù trừ 131 ↔ 331 của cùng một đối tác là hai dòng của nhánh này. Đối
    -- trừ ở đây gắn vào DÒNG định khoản chứ không vào chứng từ, nhưng bảng
    -- giữ sẵn `voucher_id` nên phép cộng không phải join thêm bảng dòng.
    SELECT s.target_kind,
           s.target_id,
           s.amount_fc,
           s.amount - s.fx_diff,
           v.branch_id,
           v.voucher_no
    FROM gl_journal_settlements s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE v.status = 2
),
settled AS (
    SELECT target_kind,
           target_id,
           SUM(amount_fc)                        AS settlement_fc,
           SUM(recognised)                       AS settlement_amount,
           string_agg(DISTINCT voucher_no, ', ') AS voucher_nos
    FROM settlement_rows
    WHERE branch_id = :branch_id
    GROUP BY target_kind, target_id
),
subledger AS (
    SELECT l.target_kind      AS target_kind,
           l.id               AS target_id,
           'ar_ap_ledger'     AS source_table,
           l.document_no      AS document_no,
           l.settled_fc       AS recorded_fc,
           l.settled          AS recorded
    FROM ar_ap_ledger l
    WHERE l.branch_id = :branch_id
      -- Mọi loại đích sống trên bảng này, KHÔNG chỉ hai loại hóa đơn: bỏ sót
      -- 3/4 thì mỗi khoản nợ ghi tay được đối trừ thành một dòng đỏ trên dữ
      -- liệu ĐÚNG — vế đối trừ có số, vế sổ phụ không nộp đích nào để nối.
      AND l.target_kind IN (0, 1, 3, 4)
    UNION ALL
    SELECT 2,
           i.id,
           'opening_balance_invoices',
           i.invoice_no,
           i.paid_amount_fc,
           i.paid_amount
    FROM opening_balance_invoices i
    WHERE i.branch_id = :branch_id
)
SELECT COALESCE(b.target_kind, s.target_kind) AS target_kind,
       COALESCE(b.target_id, s.target_id)     AS target_id,
       b.source_table                         AS source_table,
       b.document_no                          AS document_no,
       COALESCE(b.recorded_fc, 0)             AS recorded_settled_fc,
       COALESCE(s.settlement_fc, 0)           AS settlement_rows_fc,
       COALESCE(b.recorded, 0)                AS recorded_settled,
       COALESCE(s.settlement_amount, 0)       AS settlement_rows_amount,
       -- Số chứng từ của các phiếu đang trỏ vào đích — U11: mỗi dòng lỗi phải
       -- dẫn thẳng tới chỗ sửa, và ở đây chỗ sửa là chính những chứng từ ấy.
       s.voucher_nos                          AS settlement_voucher_nos
FROM subledger b
FULL JOIN settled s
  ON s.target_kind = b.target_kind AND s.target_id = b.target_id
WHERE COALESCE(b.recorded_fc, 0) <> COALESCE(s.settlement_fc, 0)
   OR COALESCE(b.recorded, 0)    <> COALESCE(s.settlement_amount, 0)
