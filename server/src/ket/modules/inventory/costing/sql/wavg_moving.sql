-- Bình quân gia quyền TỨC THỜI (SRS 09 §3 pp.2, FR-STK-001) — một câu cho MỌI
-- khóa tồn kho cần tính của một (chi nhánh, năm tài chính); Python chỉ lặp
-- theo vòng (chuyển kho chéo khóa) và theo năm (ADR-014, LD-14).
--
-- Tham số: :branch_id, :year_start, :year_end (ranh giới ghi của niên độ —
-- horizon cắt ở cuối năm, cùng luật `lock_check`), :force_from (ngày ép tính
-- lại mọi khóa, NULL = theo dấu bẩn), :scale (số lẻ tiền).
--
-- `keys`: khóa cần tính + ngày bắt đầu = MIN(dấu bẩn, movement chưa tính sớm
-- nhất) — CTE này lặp nguyên văn ở các tệp cùng thư mục (cổng cấm ghép chuỗi
-- SQL, `test_no_sql_string_interpolation`).
--
-- `walk`: recursive CTE mang trạng thái (tồn, giá trị, bình quân gần nhất) theo
-- khóa; bước kế lấy movement TIẾP THEO bằng LATERAL trên chỉ mục khóa
-- `uq_inventory_movements_day_sequence` — không materialize sổ, không temp
-- table. Trạng thái mở đầu gộp từ toàn bộ lịch sử trước `start_date` (đã
-- COSTED theo định nghĩa của `start_date`), nên phần trước không cần đi lại.
--
-- Luật xuất (quyết định user 2026-09-19): tồn > 0 → bình quân hiện có, xuất
-- đúng bằng tồn thì lấy trọn giá trị (không rơi lẻ); tồn ≤ 0 → bình quân gần
-- nhất; chưa từng có bình quân → chờ giá (NULL). Nhập chưa giá (vế đến chuyển
-- kho ở vòng đầu, hàng bán trả lại 8C) góp số lượng, giá trị 0 — nhưng KHÔNG
-- tạo cơ sở giá: `last_unit_cost` chỉ khác NULL khi đã có một lần nhập có giá
-- (review 8B M-3: không thì mọi dòng xuất sau một lần nhập chưa giá thành
-- "đã tính" với giá 0).
--
-- Chỉ ghi dòng XUẤT có thay đổi thật (`IS DISTINCT FROM`) — `RETURNING` rỗng là
-- tín hiệu điểm bất động cho vòng chuyển kho; vế đến chuyển kho do
-- `transfer_in_legs.sql` ghi.
WITH RECURSIVE keys AS (
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
opening AS (
    SELECT k.branch_id, k.warehouse_id, k.item_id, k.lot_key, k.start_date,
           COALESCE(SUM(m.direction * m.quantity), 0) AS qty,
           COALESCE(SUM(m.direction * COALESCE(m.amount, 0)), 0) AS value,
           (
               SELECT p.unit_cost
               FROM inventory_movements p
               WHERE p.branch_id = k.branch_id
                 AND p.warehouse_id = k.warehouse_id
                 AND p.item_id = k.item_id
                 AND p.lot_key = k.lot_key
                 AND p.posting_date < k.start_date
                 AND p.unit_cost IS NOT NULL
                 AND NOT p.is_custodial
               ORDER BY p.posting_date DESC, p.sequence_in_day DESC
               LIMIT 1
           ) AS last_unit_cost
    FROM keys k
    LEFT JOIN inventory_movements m
      ON m.branch_id = k.branch_id
     AND m.warehouse_id = k.warehouse_id
     AND m.item_id = k.item_id
     AND m.lot_key = k.lot_key
     AND m.posting_date < k.start_date
     AND NOT m.is_custodial
    GROUP BY k.branch_id, k.warehouse_id, k.item_id, k.lot_key, k.start_date
),
walk AS (
    SELECT o.branch_id, o.warehouse_id, o.item_id, o.lot_key,
           o.start_date AS pos_date,
           0 AS pos_seq,
           CAST(NULL AS BIGINT) AS movement_id,
           CAST(NULL AS SMALLINT) AS direction,
           CAST(NULL AS NUMERIC) AS unit_cost,
           CAST(NULL AS NUMERIC) AS amount,
           o.qty,
           o.value,
           CASE
               WHEN o.last_unit_cost IS NULL THEN NULL
               WHEN o.qty > 0 THEN round(o.value / o.qty, 6)
               ELSE o.last_unit_cost
           END AS last_unit_cost
    FROM opening o
    UNION ALL
    SELECT w.branch_id, w.warehouse_id, w.item_id, w.lot_key,
           n.posting_date,
           n.sequence_in_day,
           n.id,
           n.direction,
           c.unit_cost,
           c.amount,
           s.qty,
           s.value,
           CASE
               WHEN COALESCE(c.unit_cost, w.last_unit_cost) IS NULL THEN NULL
               WHEN s.qty > 0 THEN round(s.value / s.qty, 6)
               ELSE COALESCE(c.unit_cost, w.last_unit_cost)
           END
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT m.id, m.posting_date, m.sequence_in_day, m.direction,
               m.quantity, m.unit_cost, m.amount
        FROM inventory_movements m
        WHERE m.branch_id = w.branch_id
          AND m.warehouse_id = w.warehouse_id
          AND m.item_id = w.item_id
          AND m.lot_key = w.lot_key
          AND (m.posting_date, m.sequence_in_day) > (w.pos_date, w.pos_seq)
          AND m.posting_date <= :year_end
          AND NOT m.is_custodial
        ORDER BY m.posting_date, m.sequence_in_day
        LIMIT 1
    ) AS n
    CROSS JOIN LATERAL (
        SELECT
            CASE
                WHEN n.direction = 1 THEN n.unit_cost
                WHEN w.last_unit_cost IS NULL THEN NULL
                WHEN w.qty > 0 THEN round(w.value / w.qty, 6)
                ELSE w.last_unit_cost
            END AS unit_cost,
            CASE
                WHEN n.direction = 1 THEN n.amount
                WHEN w.last_unit_cost IS NULL THEN NULL
                WHEN w.qty > 0 AND n.quantity = w.qty THEN w.value
                WHEN w.qty > 0 THEN round(n.quantity * w.value / w.qty, :scale)
                ELSE round(n.quantity * w.last_unit_cost, :scale)
            END AS amount
    ) AS c
    CROSS JOIN LATERAL (
        SELECT w.qty + n.direction * n.quantity AS qty,
               w.value + n.direction * COALESCE(c.amount, 0) AS value
    ) AS s
)
UPDATE inventory_movements m
SET unit_cost = w.unit_cost,
    amount = w.amount,
    cost_state = CASE WHEN w.unit_cost IS NULL THEN 0 ELSE 1 END
FROM walk w
WHERE m.id = w.movement_id
  AND w.direction = -1
  AND (
      m.unit_cost IS DISTINCT FROM w.unit_cost
      OR m.amount IS DISTINCT FROM w.amount
      OR m.cost_state <> CASE WHEN w.unit_cost IS NULL THEN 0 ELSE 1 END
  )
RETURNING m.id, m.voucher_id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key
