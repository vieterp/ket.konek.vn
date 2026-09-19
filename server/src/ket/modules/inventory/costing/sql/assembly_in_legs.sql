-- Vế NHẬP thành phẩm của phiếu LẮP RÁP (kind 3, lát 8C-2) nhận Σ giá trị các
-- vế XUẤT linh kiện cùng phiếu — SRS 09 §2.3 "giá thành phẩm = tổng giá trị
-- NVL xuất". Chạy sau câu phương pháp ở MỖI vòng, khuôn `transfer_in_legs.sql`:
-- vòng này linh kiện có giá → thành phẩm nhận giá (STALE) → vòng sau khóa thành
-- phẩm vào tập `keys` và các dòng xuất thành phẩm (kể cả làm linh kiện của một
-- phiếu lắp ráp khác — "nhiều vòng", FR-STK-005) được tính lại. Một lượt job
-- hội tụ cả chuỗi; khác tháng cùng năm nằm trọn trong horizon; thành phẩm dùng
-- ở năm sau do `mark_next_year.sql` để dấu.
--
-- Một linh kiện chưa có giá → thành phẩm về PENDING (ràng buộc
-- `costed_has_unit_cost`); `bool_and` là phép "mọi vế đều có giá".
-- Thành tiền = Σ đúng từng đồng; đơn giá = thành tiền / số lượng làm tròn 6 lẻ.
--
-- Đi từ `inventory_vouchers` (kind = 3, chỉ mục `ix_inventory_vouchers_kind`)
-- sang movement theo `voucher_id`: chi nhánh không có phiếu lắp ráp trả rỗng
-- mà không quét sổ kho.
--
-- Tham số: :branch_id, :year_start, :year_end.
UPDATE inventory_movements i
SET unit_cost = CASE WHEN s.total IS NULL THEN NULL ELSE round(s.total / i.quantity, 6) END,
    amount = s.total,
    cost_state = CASE WHEN s.total IS NULL THEN 0 ELSE 2 END
FROM (
    SELECT o.voucher_id,
           CASE WHEN bool_and(o.unit_cost IS NOT NULL) THEN SUM(o.amount) END AS total
    FROM inventory_vouchers b
    JOIN inventory_movements o ON o.voucher_id = b.id
    WHERE b.kind = 3
      AND o.direction = -1
      AND o.branch_id = :branch_id
      AND o.posting_date BETWEEN :year_start AND :year_end
    GROUP BY o.voucher_id
) s
WHERE i.voucher_id = s.voucher_id
  AND i.direction = 1
  AND i.branch_id = :branch_id
  AND i.posting_date BETWEEN :year_start AND :year_end
  AND NOT i.is_custodial
  AND (
      i.amount IS DISTINCT FROM s.total
      OR i.unit_cost IS DISTINCT FROM
         CASE WHEN s.total IS NULL THEN NULL ELSE round(s.total / i.quantity, 6) END
  )
RETURNING i.id, i.voucher_id, i.branch_id, i.warehouse_id, i.item_id, i.lot_key
