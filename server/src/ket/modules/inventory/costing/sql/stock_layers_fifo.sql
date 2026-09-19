-- Dựng lại `stock_layers` của MỘT chi nhánh từ sổ kho (FIFO): lớp = movement
-- nhập có giá; còn lại = ban đầu − phần bị các dòng xuất tiêu thụ trên trục
-- tích lũy (cùng công thức `fifo.sql`). Chạy SAU câu DELETE cùng chi nhánh
-- trong cùng transaction (`engine.rebuild_layers`). Cả chi nhánh chứ không
-- theo khóa bẩn: bảng này là dẫn xuất, dựng lại trọn vẹn rẻ hơn theo dõi khóa
-- nào đã đổi qua nhiều vòng (ghi ở phase file như điểm tối ưu sau).
--
-- Chạy với `enable_nestloop = off` như `fifo.sql` (cùng lý do).
--
-- Tham số: :branch_id.
INSERT INTO stock_layers (
    movement_id, branch_id, warehouse_id, item_id, lot_id, lot_key,
    receipt_date, sequence_in_day, original_qty, remaining_qty, unit_cost
)
WITH layers AS (
    SELECT m.id, m.branch_id, m.warehouse_id, m.item_id, m.lot_id, m.lot_key,
           m.posting_date, m.sequence_in_day, m.quantity AS original_qty, m.unit_cost,
           SUM(m.quantity) OVER w AS cum_in
    FROM inventory_movements m
    WHERE m.branch_id = :branch_id
      AND m.direction = 1
      AND NOT m.is_custodial
      AND m.unit_cost IS NOT NULL
    WINDOW w AS (
        PARTITION BY m.branch_id, m.warehouse_id, m.item_id, m.lot_key
        ORDER BY m.posting_date, m.sequence_in_day, m.id
    )
),
issues AS (
    SELECT m.branch_id, m.warehouse_id, m.item_id, m.lot_key, m.quantity,
           SUM(m.quantity) OVER w AS cum_out
    FROM inventory_movements m
    WHERE m.branch_id = :branch_id
      AND m.direction = -1
      AND NOT m.is_custodial
    WINDOW w AS (
        PARTITION BY m.branch_id, m.warehouse_id, m.item_id, m.lot_key
        ORDER BY m.posting_date, m.sequence_in_day, m.id
    )
),
consumed AS (
    SELECT l.id AS layer_id,
           SUM(LEAST(i.cum_out, l.cum_in)
               - GREATEST(i.cum_out - i.quantity, l.cum_in - l.original_qty)) AS qty
    FROM layers l
    JOIN issues i
      ON i.branch_id = l.branch_id
     AND i.warehouse_id = l.warehouse_id
     AND i.item_id = l.item_id
     AND i.lot_key = l.lot_key
     AND l.cum_in > i.cum_out - i.quantity
     AND l.cum_in - l.original_qty < i.cum_out
    GROUP BY l.id
)
SELECT l.id, l.branch_id, l.warehouse_id, l.item_id, l.lot_id, l.lot_key,
       l.posting_date, l.sequence_in_day, l.original_qty,
       GREATEST(0, l.original_qty - COALESCE(c.qty, 0)), l.unit_cost
FROM layers l
LEFT JOIN consumed c ON c.layer_id = l.id
