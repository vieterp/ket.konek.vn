-- Mảnh SQL dùng chung — **không phải một dataset**. Nhúng bằng
-- `-- #include: settled_as_of.sql` (xem `loader._expand_includes`).
--
-- Thân của CTE `settled_as_of`: một khoản nợ đã được đối trừ bao nhiêu, tính
-- tới `:to_date`. Mọi dataset công nợ hỏi đúng câu này, và trước lát 7G-2b nó
-- có hai bản chép vì tệp dataset chưa include được nhau — hai bản ấy đã lệch
-- thật hai lần trong một vòng review (7G-1).
--
-- Tham số mà mảnh này đòi tệp gọi phải khai: `:to_date`.
-- Đã đối trừ bao nhiêu, tính tới `:to_date`. Năm bảng vì có năm phân hệ ghi
-- lượt đối trừ; cả năm mang đúng một bộ cột, nên khối này là một phép cộng
-- chứ không phải năm.
--
-- **`amount - fx_diff`, không phải `amount`.** Cột `amount` của bảng đối trừ
-- là VND theo tỷ giá **THANH TOÁN**; phần VND thật sự giải phóng trên sổ là
-- `amount - fx_diff`, và đó đúng là con số `apply_settlement_rows` cộng vào
-- khoản đích (phần chênh đi vào 515/635 — FR-SYS-066). Cộng `amount` trần
-- làm cột VND lệch trên **mọi** khoản nợ ngoại tệ trả từng phần, và lệch
-- theo hướng tệ nhất: `settled` vượt `amount` thì `remaining` ra số ÂM trên
-- một khoản đã tất toán. Cơ sở ghi nhận cũng là điều docstring dưới đây
-- hứa, và là điều bản 7A làm đúng bằng `amount - settled`.
SELECT s.target_kind,
       s.target_id,
       SUM(s.amount_fc)          AS settled_fc,
       SUM(s.amount - s.fx_diff) AS settled
FROM (
    SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
      FROM cash_settlements
    UNION ALL
    SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
      FROM bank_settlements
    UNION ALL
    SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
      FROM purchase_settlements
    UNION ALL
    SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
      FROM sales_settlements
    UNION ALL
    SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
      FROM gl_journal_settlements
) AS s
-- `EXISTS` chứ không `JOIN`: khối này bị vật chất hóa (có hàm gộp, tham
-- chiếu hai lần), và một `JOIN vouchers` buộc planner dựng hash của **mọi**
-- chứng từ đã ghi sổ cho một báo cáo ra vài chục dòng. `EXISTS` đi khóa
-- chính từng dòng đối trừ.
WHERE EXISTS (
    SELECT 1
    FROM vouchers sv
    WHERE sv.id = s.voucher_id
      AND sv.status = 2
      AND sv.posting_date <= :to_date
)
GROUP BY s.target_kind, s.target_id
