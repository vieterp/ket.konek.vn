-- Dựng lại `stock_layers` của MỘT chi nhánh (đích danh): còn lại = ban đầu −
-- Σ số lượng các dòng xuất trỏ thẳng tới lần nhập. Chạy sau câu DELETE cùng
-- chi nhánh trong cùng transaction (`engine.rebuild_layers`).
--
-- Tham số: :branch_id.
INSERT INTO stock_layers (
    movement_id, branch_id, warehouse_id, item_id, lot_id, lot_key,
    receipt_date, sequence_in_day, original_qty, remaining_qty, unit_cost
)
SELECT l.id, l.branch_id, l.warehouse_id, l.item_id, l.lot_id, l.lot_key,
       l.posting_date, l.sequence_in_day, l.quantity,
       GREATEST(0, l.quantity - COALESCE(c.qty, 0)), l.unit_cost
FROM inventory_movements l
LEFT JOIN (
    SELECT source_movement_id, SUM(quantity) AS qty
    FROM inventory_movements
    WHERE branch_id = :branch_id
      AND direction = -1
      AND NOT is_custodial
      AND source_movement_id IS NOT NULL
    GROUP BY source_movement_id
) AS c ON c.source_movement_id = l.id
WHERE l.branch_id = :branch_id
  AND l.direction = 1
  AND NOT l.is_custodial
  AND l.unit_cost IS NOT NULL
