-- Sổ kho của thủ kho khớp sổ kế toán kho về SỐ LƯỢNG (BR-STK-02, FR-WHK-022 —
-- lát 8D). Khuôn `treasurer_book_matches_ledger.sql` của 6C.
--
-- Hai sổ song song CÓ THỂ lệch một cách hợp lệ: phiếu đã ghi sổ kế toán mà thủ
-- kho chưa ghi sổ kho (`keeper_status = 0`, chờ) là chênh lệch NGHIỆP VỤ —
-- việc của báo cáo chênh lệch (lát 8F), không phải của check này. Check này chỉ
-- bắt trạng thái KHÔNG BAO GIỜ hợp lệ:
--
-- * dòng sổ kho trỏ vào phiếu không-còn-ghi-sổ (hoặc không phải phiếu kho) —
--   đường bỏ ghi sổ phải gỡ dòng sổ kho theo (`keeper.clear_after_unpost`);
-- * phiếu đã ghi sổ kho mà số lượng vào/ra trên sổ kho lệch sổ kế toán kho
--   (`inventory_movements`) — hai sổ cùng đơn vị chính;
-- * dòng sổ kho tồn tại nhưng phiếu vẫn mang trạng thái "chờ" — ai đó ghi sổ
--   vòng qua đường lật trạng thái;
-- * phiếu mang "đã ghi sổ kho"/"không áp dụng" (đã ghi sổ kế toán, có movement)
--   mà không có dòng sổ kho nào.
--
-- Hàng giữ hộ vào CẢ HAI sổ như hàng của mình: sổ kho là sổ số lượng, và thủ
-- kho giữ cả hai loại trên cùng một kệ (BR-STK-07 nói về giá trị).
--
-- Trạng thái: vouchers.status 2 = Đã ghi sổ; keeper_status 0 chờ / 1 đã ghi sổ
-- kho / 2 không áp dụng (phân hệ tắt). direction 1 nhập / -1 xuất.
WITH ledger AS (
    SELECT iv.id AS voucher_id,
           v.status AS voucher_status,
           iv.keeper_status,
           COALESCE(SUM(m.quantity) FILTER (WHERE m.direction = 1), 0) AS ledger_in,
           COALESCE(SUM(m.quantity) FILTER (WHERE m.direction = -1), 0) AS ledger_out,
           COUNT(m.id) AS movements
    FROM inventory_vouchers iv
    JOIN vouchers v ON v.id = iv.id
    LEFT JOIN inventory_movements m ON m.voucher_id = iv.id
    WHERE v.branch_id = :branch_id
    GROUP BY iv.id, v.status, iv.keeper_status
),
book AS (
    SELECT w.voucher_id,
           SUM(w.in_qty) AS book_in,
           SUM(w.out_qty) AS book_out
    FROM warehouse_book w
    WHERE w.branch_id = :branch_id
    GROUP BY w.voucher_id
)
SELECT COALESCE(l.voucher_id, b.voucher_id) AS voucher_id,
       l.voucher_status,
       l.keeper_status,
       b.book_in,
       b.book_out,
       l.ledger_in,
       l.ledger_out
FROM ledger l
FULL JOIN book b ON b.voucher_id = l.voucher_id
WHERE (b.voucher_id IS NOT NULL AND (l.voucher_id IS NULL OR l.voucher_status <> 2))
   OR (b.voucher_id IS NOT NULL AND l.voucher_status = 2
       AND (b.book_in <> l.ledger_in OR b.book_out <> l.ledger_out))
   OR (b.voucher_id IS NOT NULL AND l.keeper_status = 0)
   OR (b.voucher_id IS NULL AND l.voucher_status = 2
       AND l.keeper_status IN (1, 2) AND l.movements > 0)
