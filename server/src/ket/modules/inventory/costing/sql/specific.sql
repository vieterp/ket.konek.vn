-- Đích danh (SRS 09 §3 pp.4): dòng xuất lấy giá của đúng lần nhập nó chỉ
-- (`source_movement_id`, service đã kiểm cùng khóa lúc cất). Lần nhập chưa có
-- giá, hoặc dòng xuất không chỉ lần nhập (phiếu sinh từ hóa đơn bán — chưa có
-- chỗ chọn, 8C/8G) → chờ giá.
--
-- Tham số: :branch_id, :year_start, :year_end, :force_from, :scale.
-- CTE `keys` lặp nguyên văn từ `wavg_moving.sql`.
WITH keys AS (
    SELECT branch_id, warehouse_id, item_id, lot_key, MIN(start_date) AS start_date
    FROM (
        SELECT branch_id, warehouse_id, item_id, lot_key, from_date AS start_date
        FROM inventory_recalc_queue
        WHERE branch_id = :branch_id
          AND from_date BETWEEN :year_start AND :year_end
        UNION ALL
        SELECT branch_id, warehouse_id, item_id, lot_key, MIN(posting_date)
        FROM inventory_movements
        WHERE branch_id = :branch_id
          AND posting_date BETWEEN :year_start AND :year_end
          AND cost_state <> 1
          AND NOT is_custodial
        GROUP BY branch_id, warehouse_id, item_id, lot_key
        UNION ALL
        SELECT DISTINCT branch_id, warehouse_id, item_id, lot_key, CAST(:force_from AS DATE)
        FROM inventory_movements
        WHERE CAST(:force_from AS DATE) IS NOT NULL
          AND branch_id = :branch_id
          AND posting_date BETWEEN CAST(:force_from AS DATE) AND :year_end
          AND NOT is_custodial
    ) AS candidates
    GROUP BY branch_id, warehouse_id, item_id, lot_key
),
priced AS (
    SELECT m.id, m.quantity, s.unit_cost,
           CASE WHEN s.unit_cost IS NULL THEN NULL
                ELSE round(m.quantity * s.unit_cost, :scale) END AS amount
    FROM inventory_movements m
    JOIN keys k
      ON k.branch_id = m.branch_id
     AND k.warehouse_id = m.warehouse_id
     AND k.item_id = m.item_id
     AND k.lot_key = m.lot_key
    LEFT JOIN inventory_movements s
      ON s.id = m.source_movement_id
     AND s.direction = 1
     AND s.unit_cost IS NOT NULL
    WHERE m.direction = -1
      AND NOT m.is_custodial
      AND m.posting_date >= k.start_date
      AND m.posting_date <= :year_end
)
UPDATE inventory_movements m
SET unit_cost = p.unit_cost,
    amount = p.amount,
    cost_state = CASE WHEN p.unit_cost IS NULL THEN 0 ELSE 1 END
FROM priced p
WHERE m.id = p.id
  AND (
      m.unit_cost IS DISTINCT FROM p.unit_cost
      OR m.amount IS DISTINCT FROM p.amount
      OR m.cost_state <> CASE WHEN p.unit_cost IS NULL THEN 0 ELSE 1 END
  )
RETURNING m.id, m.voucher_id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key
