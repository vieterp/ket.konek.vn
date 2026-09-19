-- Snapshot tồn kho theo kỳ (`inventory_balances`, bước 10 phase 8) cho MỘT
-- (chi nhánh, kỳ) bằng một câu INSERT … SELECT từ sổ kho: đầu kỳ = Σ trước
-- ngày đầu kỳ, nhập/xuất = Σ trong kỳ, cuối kỳ = tổng. Chạy SAU câu DELETE
-- cùng (chi nhánh, kỳ) trong cùng transaction — cặp DELETE + INSERT thay cho
-- ON CONFLICT vì cùng lỗ dòng mồ côi mà `posting.balances.recalc` đã gặp.
-- Hàng giữ hộ (`is_custodial`) không vào đây: bảng này là số sổ kế toán kho.
--
-- Tham số: :branch_id, :period_id, :period_start, :period_end.
INSERT INTO inventory_balances (
    period_id, branch_id, warehouse_id, item_id, lot_id, serial_id, lot_key, serial_key,
    opening_qty, opening_value, in_qty, in_value, out_qty, out_value,
    closing_qty, closing_value, computed_at
)
SELECT :period_id, branch_id, warehouse_id, item_id, lot_id, serial_id,
       lot_key, COALESCE(serial_id, 0),
       SUM(CASE WHEN posting_date < :period_start THEN direction * quantity ELSE 0 END),
       SUM(CASE WHEN posting_date < :period_start THEN direction * COALESCE(amount, 0) ELSE 0 END),
       SUM(CASE WHEN posting_date >= :period_start AND direction = 1 THEN quantity ELSE 0 END),
       SUM(CASE WHEN posting_date >= :period_start AND direction = 1 THEN COALESCE(amount, 0) ELSE 0 END),
       SUM(CASE WHEN posting_date >= :period_start AND direction = -1 THEN quantity ELSE 0 END),
       SUM(CASE WHEN posting_date >= :period_start AND direction = -1 THEN COALESCE(amount, 0) ELSE 0 END),
       SUM(direction * quantity),
       SUM(direction * COALESCE(amount, 0)),
       clock_timestamp()
FROM inventory_movements
WHERE branch_id = :branch_id
  AND posting_date <= :period_end
  AND NOT is_custodial
GROUP BY branch_id, warehouse_id, item_id, lot_id, serial_id, lot_key
