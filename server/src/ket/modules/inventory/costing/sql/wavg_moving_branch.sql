-- Bình quân gia quyền TỨC THỜI, phạm vi KHÔNG THEO KHO (SRS 09 §3, FR-STK-007,
-- lát 8C-1): khóa tính giá là (chi nhánh, mã hàng, lô) — mọi kho của chi nhánh
-- góp vào một bình quân; movement vẫn mang kho riêng (tồn từng kho là số lượng,
-- giá là của cả nhóm). Cùng cấu trúc `wavg_moving.sql`, khác ba chỗ:
--
-- * `keys`/`opening`/`walk` gom theo (branch_id, item_id, lot_key) — dấu bẩn
--   của bất kỳ kho nào kéo cả nhóm vào lượt tính;
-- * bước LATERAL lấy movement kế theo bộ `(posting_date, sequence_in_day,
--   warehouse_id, id)` trên chỉ mục `ix_inventory_movements_branch_item_order`
--   — `sequence_in_day` chỉ có nghĩa trong một kho, nên thứ tự trong ngày
--   GIỮA các kho là số thứ tự rồi kho rồi id (giới hạn có chủ đích; sắp xếp
--   lại trong ngày FR-STK-017 vẫn theo kho);
-- * chuyển kho nội bộ trong cùng nhóm: vế đi nhận bình quân nhóm, vế đến
--   nhận giá vế đi (`transfer_in_legs.sql`) — giá trị nhóm không đổi, hội tụ
--   qua vòng như chuyển kho chéo khóa.
--
-- Tham số và luật xuất như `wavg_moving.sql`.
WITH RECURSIVE keys AS (
    SELECT branch_id, item_id, lot_key, MIN(start_date) AS start_date
    FROM (
        SELECT branch_id, item_id, lot_key, from_date AS start_date
        FROM inventory_recalc_queue
        WHERE branch_id = :branch_id
          AND from_date BETWEEN :year_start AND :year_end
        UNION ALL
        SELECT branch_id, item_id, lot_key, MIN(posting_date)
        FROM inventory_movements
        WHERE branch_id = :branch_id
          AND posting_date BETWEEN :year_start AND :year_end
          AND cost_state <> 1
          AND NOT is_custodial
        GROUP BY branch_id, item_id, lot_key
        UNION ALL
        SELECT DISTINCT branch_id, item_id, lot_key, CAST(:force_from AS DATE)
        FROM inventory_movements
        WHERE CAST(:force_from AS DATE) IS NOT NULL
          AND branch_id = :branch_id
          AND posting_date BETWEEN CAST(:force_from AS DATE) AND :year_end
          AND NOT is_custodial
    ) AS candidates
    GROUP BY branch_id, item_id, lot_key
),
opening AS (
    SELECT k.branch_id, k.item_id, k.lot_key, k.start_date,
           COALESCE(SUM(m.direction * m.quantity), 0) AS qty,
           COALESCE(SUM(m.direction * COALESCE(m.amount, 0)), 0) AS value,
           (
               SELECT p.unit_cost
               FROM inventory_movements p
               WHERE p.branch_id = k.branch_id
                 AND p.item_id = k.item_id
                 AND p.lot_key = k.lot_key
                 AND p.posting_date < k.start_date
                 AND p.unit_cost IS NOT NULL
                 AND NOT p.is_custodial
               ORDER BY p.posting_date DESC, p.sequence_in_day DESC, p.warehouse_id DESC, p.id DESC
               LIMIT 1
           ) AS last_unit_cost
    FROM keys k
    LEFT JOIN inventory_movements m
      ON m.branch_id = k.branch_id
     AND m.item_id = k.item_id
     AND m.lot_key = k.lot_key
     AND m.posting_date < k.start_date
     AND NOT m.is_custodial
    GROUP BY k.branch_id, k.item_id, k.lot_key, k.start_date
),
walk AS (
    SELECT o.branch_id, o.item_id, o.lot_key,
           o.start_date AS pos_date,
           0 AS pos_seq,
           0 AS pos_warehouse,
           CAST(0 AS BIGINT) AS pos_id,
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
    SELECT w.branch_id, w.item_id, w.lot_key,
           n.posting_date,
           n.sequence_in_day,
           n.warehouse_id,
           n.id,
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
        SELECT m.id, m.posting_date, m.sequence_in_day, m.warehouse_id, m.direction,
               m.quantity, m.unit_cost, m.amount
        FROM inventory_movements m
        WHERE m.branch_id = w.branch_id
          AND m.item_id = w.item_id
          AND m.lot_key = w.lot_key
          AND (m.posting_date, m.sequence_in_day, m.warehouse_id, m.id)
              > (w.pos_date, w.pos_seq, w.pos_warehouse, w.pos_id)
          AND m.posting_date <= :year_end
          AND NOT m.is_custodial
        ORDER BY m.posting_date, m.sequence_in_day, m.warehouse_id, m.id
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
