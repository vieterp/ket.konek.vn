-- Nhập trước – xuất trước (SRS 09 §3 pp.3) bằng TỔNG TÍCH LŨY, không đệ quy:
-- lớp = movement nhập có giá, xếp theo thứ tự khóa với `cum_in`; xuất xếp
-- theo thứ tự khóa với `cum_out`; dòng xuất i tiêu thụ đoạn (cum_out − qty,
-- cum_out] trên trục tích lũy, lớp L phủ đoạn (cum_in − original, cum_in] —
-- phần giao hai đoạn là số lượng lấy từ lớp ấy. Tính trên CẢ lịch sử khóa (hàm
-- xác định của lịch sử nên phần trước `start_date` ra đúng số cũ), chỉ GHI
-- dòng trong horizon. Lớp đầu năm không cần "chuyển năm": lịch sử liền.
--
-- Luật vượt tồn (quyết định user 2026-09-19): xuất khi chưa đủ lớp "ăn" lớp
-- nhập SAU ngày xuất (nhập bù — trục tích lũy không nhìn ngày); phần đuôi vượt
-- mọi lớp lấy giá lớp cuối; khóa chưa có lớp nào → chờ giá.
--
-- Engine chạy câu này với `enable_nestloop = off` (SET LOCAL, xem `engine.py`):
-- planner ước lượng CTE `keys` sai (1–2 dòng) và chọn nested loop giữa hai CTE
-- 50.000 dòng — spike 8B đo hàng chục phút; hash/merge join là dưới một giây.
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
layers AS (
    SELECT m.id AS layer_id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key,
           m.posting_date, m.sequence_in_day, m.quantity AS original_qty, m.unit_cost,
           SUM(m.quantity) OVER w AS cum_in
    FROM inventory_movements m
    JOIN keys k
      ON k.branch_id = m.branch_id
     AND k.warehouse_id = m.warehouse_id
     AND k.item_id = m.item_id
     AND k.lot_key = m.lot_key
    WHERE m.direction = 1
      AND NOT m.is_custodial
      AND m.unit_cost IS NOT NULL
    WINDOW w AS (
        PARTITION BY m.branch_id, m.warehouse_id, m.item_id, m.lot_key
        ORDER BY m.posting_date, m.sequence_in_day, m.id
    )
),
issues AS (
    SELECT m.id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key,
           m.quantity, m.posting_date, k.start_date,
           SUM(m.quantity) OVER w AS cum_out
    FROM inventory_movements m
    JOIN keys k
      ON k.branch_id = m.branch_id
     AND k.warehouse_id = m.warehouse_id
     AND k.item_id = m.item_id
     AND k.lot_key = m.lot_key
    WHERE m.direction = -1
      AND NOT m.is_custodial
    WINDOW w AS (
        PARTITION BY m.branch_id, m.warehouse_id, m.item_id, m.lot_key
        ORDER BY m.posting_date, m.sequence_in_day, m.id
    )
),
priced AS (
    -- Một phép giao lớp × xuất theo khóa, gộp ngay theo dòng xuất. Giá lớp cuối
    -- lấy bằng dò chỉ mục trên bảng gốc (không join CTE với CTE: planner ước
    -- lượng CTE `keys` sai và biến join ấy thành nested loop 50k × 50k — spike 8B).
    -- `LEAST`/`GREATEST` của PostgreSQL BỎ QUA NULL: dòng xuất không khớp lớp nào
    -- (LEFT JOIN trả lớp NULL) sẽ cho phần giao = chính số lượng xuất nếu không
    -- rẽ nhánh tường minh — bài "chưa có lớp thì chờ giá" bắt được.
    SELECT i.id, i.quantity,
           COALESCE(SUM(CASE WHEN l.layer_id IS NULL THEN 0 ELSE
               LEAST(i.cum_out, l.cum_in)
               - GREATEST(i.cum_out - i.quantity, l.cum_in - l.original_qty) END), 0) AS covered,
           COALESCE(SUM(CASE WHEN l.layer_id IS NULL THEN 0 ELSE
               (LEAST(i.cum_out, l.cum_in)
               - GREATEST(i.cum_out - i.quantity, l.cum_in - l.original_qty)) * l.unit_cost END), 0)
               AS covered_value,
           (
               SELECT p.unit_cost
               FROM inventory_movements p
               WHERE p.branch_id = i.branch_id
                 AND p.warehouse_id = i.warehouse_id
                 AND p.item_id = i.item_id
                 AND p.lot_key = i.lot_key
                 AND p.direction = 1
                 AND p.unit_cost IS NOT NULL
                 AND NOT p.is_custodial
               ORDER BY p.posting_date DESC, p.sequence_in_day DESC, p.id DESC
               LIMIT 1
           ) AS last_unit_cost
    FROM issues i
    LEFT JOIN layers l
      ON l.branch_id = i.branch_id
     AND l.warehouse_id = i.warehouse_id
     AND l.item_id = i.item_id
     AND l.lot_key = i.lot_key
     AND l.cum_in > i.cum_out - i.quantity
     AND l.cum_in - l.original_qty < i.cum_out
    WHERE i.posting_date >= i.start_date
      AND i.posting_date <= :year_end
    GROUP BY i.id, i.quantity, i.branch_id, i.warehouse_id, i.item_id, i.lot_key
),
amounts AS (
    SELECT id, quantity,
           CASE
               WHEN covered >= quantity THEN round(covered_value, :scale)
               WHEN last_unit_cost IS NOT NULL
                   THEN round(covered_value + (quantity - covered) * last_unit_cost, :scale)
               ELSE NULL
           END AS amount
    FROM priced
)
UPDATE inventory_movements m
SET amount = p.amount,
    unit_cost = CASE WHEN p.amount IS NULL THEN NULL ELSE round(p.amount / p.quantity, 6) END,
    cost_state = CASE WHEN p.amount IS NULL THEN 0 ELSE 1 END
FROM amounts p
WHERE m.id = p.id
  AND (
      m.amount IS DISTINCT FROM p.amount
      OR m.unit_cost IS DISTINCT FROM
         CASE WHEN p.amount IS NULL THEN NULL ELSE round(p.amount / p.quantity, 6) END
      OR m.cost_state <> CASE WHEN p.amount IS NULL THEN 0 ELSE 1 END
  )
RETURNING m.id, m.voucher_id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key
