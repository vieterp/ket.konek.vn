-- Hàng bán trả lại ở NĂM SAU lần xuất (FR-STK-004, lát 8C-1): horizon ghi cắt
-- cuối năm nên `return_in_legs.sql` của năm này không chạm lần nhập năm sau,
-- mà giá lần xuất vừa đổi thì giá nhập ấy đã lệch. Để lại dấu bẩn tại ngày
-- nhập (kẹp vào kỳ mở sớm nhất như `mark_next_year.sql`) cho khóa của lần
-- nhập — lượt sau (hoặc năm sau trong cùng lượt) chép giá và tính lại các
-- dòng xuất đứng sau nó.
--
-- Tham số: :branch_id, :year_start, :year_end, :marks_version.
INSERT INTO inventory_recalc_queue (
    branch_id, warehouse_id, item_id, lot_id, lot_key, from_date, marked_at, reason
)
SELECT i.branch_id, i.warehouse_id, i.item_id, MIN(i.lot_id), i.lot_key,
       GREATEST(
           MIN(i.posting_date),
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
       'return receipt after source year'
FROM inventory_movements i
JOIN inventory_movements o ON o.id = i.source_movement_id
WHERE i.branch_id = :branch_id
  AND i.direction = 1
  AND o.direction = -1
  AND i.posting_date > :year_end
  AND o.posting_date BETWEEN :year_start AND :year_end
  AND NOT i.is_custodial
  AND i.unit_cost IS DISTINCT FROM o.unit_cost
GROUP BY i.branch_id, i.warehouse_id, i.item_id, i.lot_key
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
