-- Ranh giới năm (review 8B M-1): horizon ghi cắt ở cuối niên độ, nhưng khóa
-- vừa tính lại mà còn movement ở năm sau thì giá năm sau đã lệch (tồn đầu năm
-- sau đổi). Thay vì để số sai im lặng tới `carry_forward` 8C, để lại một dấu
-- bẩn ở ngày đầu năm sau — kẹp vào kỳ mở sớm nhất như `movements.mark_recalc`
-- — cho lượt sau (hoặc năm sau trong cùng lượt) nhặt. Upsert giữ MIN(from_date),
-- làm mới `marked_at` (dấu mới không bị lượt này xóa: phiên bản đã đọc khác).
--
-- Tham số: :branch_id, :year_start, :year_end, :force_from.
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
)
INSERT INTO inventory_recalc_queue (
    branch_id, warehouse_id, item_id, lot_id, lot_key, from_date, marked_at, reason
)
SELECT k.branch_id, k.warehouse_id, k.item_id, MIN(m.lot_id), k.lot_key,
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
FROM keys k
JOIN inventory_movements m
  ON m.branch_id = k.branch_id
 AND m.warehouse_id = k.warehouse_id
 AND m.item_id = k.item_id
 AND m.lot_key = k.lot_key
 AND m.posting_date > :year_end
 AND NOT m.is_custodial
GROUP BY k.branch_id, k.warehouse_id, k.item_id, k.lot_key
ON CONFLICT ON CONSTRAINT uq_inventory_recalc_queue_key DO UPDATE
SET from_date = LEAST(inventory_recalc_queue.from_date, EXCLUDED.from_date),
    marked_at = clock_timestamp(),
    reason = EXCLUDED.reason
