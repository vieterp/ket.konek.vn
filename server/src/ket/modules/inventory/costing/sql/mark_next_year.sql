-- Ranh giới năm (review 8B M-1): horizon ghi cắt ở cuối niên độ, nhưng khóa
-- vừa tính lại mà còn movement ở năm sau thì giá năm sau đã lệch (tồn đầu năm
-- sau đổi). Thay vì để số sai im lặng tới `carry_forward` 8C, để lại một dấu
-- bẩn ở ngày đầu năm sau — kẹp vào kỳ mở sớm nhất như `movements.mark_recalc`
-- — cho lượt sau (hoặc năm sau trong cùng lượt) nhặt. Upsert giữ MIN(from_date),
-- làm mới `marked_at` (dấu mới không bị lượt này xóa: phiên bản đã đọc khác).
--
-- Tham số: :branch_id, :year_start, :year_end, :force_from, :by_warehouse, :marks_version.
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
INSERT INTO inventory_recalc_queue (
    branch_id, warehouse_id, item_id, lot_id, lot_key, from_date, marked_at, reason
)
SELECT m.branch_id, m.warehouse_id, m.item_id, MIN(m.lot_id), m.lot_key,
       GREATEST(
           CAST(:year_end AS DATE) + 1,
           (
               SELECT MIN(p.start_date)
               FROM accounting_periods p
               JOIN fiscal_years y ON y.id = p.fiscal_year_id
               WHERE p.locked_at IS NULL
                 AND NOT y.is_closed
                 AND p.end_date > :year_end
           )
       ),
       clock_timestamp(),
       'carry into next year'
FROM groups k
JOIN inventory_movements m
  ON m.branch_id = k.branch_id
 AND (k.warehouse_id IS NULL OR k.warehouse_id = m.warehouse_id)
 AND m.item_id = k.item_id
 AND m.lot_key = k.lot_key
 AND m.posting_date > :year_end
 AND NOT m.is_custodial
GROUP BY m.branch_id, m.warehouse_id, m.item_id, m.lot_key
ON CONFLICT ON CONSTRAINT uq_inventory_recalc_queue_key DO UPDATE
SET from_date = CASE
        -- Dấu cùng khóa vừa được lượt này đọc và tính xong (phiên bản ≤ mốc đã
        -- đọc): việc của nó ở năm này đã làm, đổi hẳn sang đầu năm sau. LEAST ở
        -- đây là giữ ngày cũ với phiên bản mới — dấu không bao giờ xóa được.
        WHEN CAST(:marks_version AS TIMESTAMPTZ) IS NOT NULL
             AND inventory_recalc_queue.marked_at <= CAST(:marks_version AS TIMESTAMPTZ)
            THEN EXCLUDED.from_date
        ELSE LEAST(inventory_recalc_queue.from_date, EXCLUDED.from_date)
    END,
    marked_at = clock_timestamp(),
    reason = EXCLUDED.reason
