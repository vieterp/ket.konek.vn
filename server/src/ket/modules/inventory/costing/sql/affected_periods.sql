-- FR-STK-003 / RT-11 — tổng hợp movement trong horizon của các khóa cần tính
-- theo KỲ, cộng một dòng tổng (GROUPING SETS): job hỏi "kỳ nào đã khóa bị chạm",
-- xem trước hỏi "bao nhiêu khóa / dòng / chứng từ, từ ngày nào". Gộp ở SQL,
-- không kéo từng movement về Python (review 8B M-5, ADR-014).
--
-- Dòng kỳ: period_id, period_no, period_locked, movements. Dòng tổng:
-- period_id NULL, keys (đếm khóa khác nhau), movements, vouchers, earliest.
--
-- Tham số: :branch_id, :year_start, :year_end, :force_from, :by_warehouse.
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
groups AS (
    -- Khóa GIÁ: theo kho như thường; năm bình quân "không theo kho" (8C-1,
    -- :by_warehouse = false) gom mọi kho của (chi nhánh, mã hàng, lô) — dấu ở
    -- kho A kéo movement kho B vào horizon (review 8C-1 H-4/M-1).
    SELECT branch_id,
           CASE WHEN CAST(:by_warehouse AS BOOLEAN) THEN warehouse_id END AS warehouse_id,
           item_id, lot_key, MIN(start_date) AS start_date
    FROM keys
    GROUP BY branch_id,
             CASE WHEN CAST(:by_warehouse AS BOOLEAN) THEN warehouse_id END,
             item_id, lot_key
)
SELECT p.id AS period_id,
       p.period_no,
       (p.locked_at IS NOT NULL) AS period_locked,
       COUNT(*) AS movements,
       COUNT(DISTINCT (m.warehouse_id, m.item_id, m.lot_key)) AS keys,
       COUNT(DISTINCT m.voucher_id) AS vouchers,
       MIN(k.start_date) AS earliest
FROM inventory_movements m
JOIN groups k
  ON k.branch_id = m.branch_id
 AND (k.warehouse_id IS NULL OR k.warehouse_id = m.warehouse_id)
 AND k.item_id = m.item_id
 AND k.lot_key = m.lot_key
JOIN accounting_periods p ON p.id = m.period_id
WHERE m.posting_date >= k.start_date
  AND m.posting_date <= :year_end
  AND NOT m.is_custodial
GROUP BY GROUPING SETS ((p.id, p.period_no, p.locked_at), ())
ORDER BY p.period_no NULLS LAST
