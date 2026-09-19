-- Bình quân gia quyền CUỐI KỲ, phạm vi KHÔNG THEO KHO (SRS 09 §3, FR-STK-007,
-- lát 8C-1): một bình quân kỳ cho mọi kho của (chi nhánh, mã hàng, lô). Cùng
-- cấu trúc `wavg_period.sql`, chỉ bỏ `warehouse_id` khỏi khóa của `keys`/
-- `periods`/`flows`/`opening`/`walk`; dòng xuất ở kho nào cũng nhận giá kỳ của
-- nhóm. "Đa chi nhánh tính chung cho các chi nhánh phụ thuộc" (SRS) KHÔNG làm —
-- job per-branch dưới RLS (ghi hoãn ở phase file).
--
-- Tham số và luật như `wavg_period.sql`.
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
periods AS (
    SELECT k.branch_id, k.item_id, k.lot_key,
           p.id AS period_id, p.start_date AS period_start,
           row_number() OVER (
               PARTITION BY k.branch_id, k.item_id, k.lot_key
               ORDER BY p.start_date
           ) AS rn
    FROM keys k
    JOIN accounting_periods p
      ON p.fiscal_year_id = :fiscal_year_id
     AND p.end_date >= k.start_date
),
flows AS (
    SELECT pr.branch_id, pr.item_id, pr.lot_key,
           pr.period_id, pr.period_start, pr.rn,
           COALESCE(SUM(CASE WHEN m.direction = 1 THEN m.quantity END), 0) AS in_qty,
           COALESCE(SUM(CASE WHEN m.direction = 1 THEN COALESCE(m.amount, 0) END), 0) AS in_value,
           -- Cơ sở giá của kỳ: có ít nhất một lần nhập CÓ giá (review 8B M-3).
           COUNT(m.id) FILTER (WHERE m.direction = 1 AND m.unit_cost IS NOT NULL) AS costed_in
    FROM periods pr
    LEFT JOIN inventory_movements m
      ON m.branch_id = pr.branch_id
     AND m.item_id = pr.item_id
     AND m.lot_key = pr.lot_key
     AND m.period_id = pr.period_id
     AND NOT m.is_custodial
    GROUP BY pr.branch_id, pr.item_id, pr.lot_key,
             pr.period_id, pr.period_start, pr.rn
),
opening AS (
    SELECT f.branch_id, f.item_id, f.lot_key,
           COALESCE(SUM(m.direction * m.quantity), 0) AS qty,
           COALESCE(SUM(m.direction * COALESCE(m.amount, 0)), 0) AS value,
           (
               SELECT p.unit_cost
               FROM inventory_movements p
               WHERE p.branch_id = f.branch_id
                 AND p.item_id = f.item_id
                 AND p.lot_key = f.lot_key
                 AND p.posting_date < f.period_start
                 AND p.unit_cost IS NOT NULL
                 AND NOT p.is_custodial
               ORDER BY p.posting_date DESC, p.sequence_in_day DESC, p.warehouse_id DESC, p.id DESC
               LIMIT 1
           ) AS last_unit_cost
    FROM flows f
    LEFT JOIN inventory_movements m
      ON m.branch_id = f.branch_id
     AND m.item_id = f.item_id
     AND m.lot_key = f.lot_key
     AND m.posting_date < f.period_start
     AND NOT m.is_custodial
    WHERE f.rn = 1
    GROUP BY f.branch_id, f.item_id, f.lot_key, f.period_start
),
walk AS (
    SELECT o.branch_id, o.item_id, o.lot_key,
           CAST(0 AS BIGINT) AS rn,
           CAST(NULL AS INTEGER) AS period_id,
           o.qty,
           o.value,
           CASE
               WHEN o.last_unit_cost IS NULL THEN NULL
               WHEN o.qty > 0 THEN round(o.value / o.qty, 6)
               ELSE o.last_unit_cost
           END AS avg_cost
    FROM opening o
    UNION ALL
    SELECT w.branch_id, w.item_id, w.lot_key,
           f.rn,
           f.period_id,
           w.qty + f.in_qty - COALESCE(x.out_qty, 0),
           w.value + f.in_value - COALESCE(x.out_value, 0),
           a.avg_cost
    FROM walk w
    JOIN flows f
      ON f.branch_id = w.branch_id
     AND f.item_id = w.item_id
     AND f.lot_key = w.lot_key
     AND f.rn = w.rn + 1
    CROSS JOIN LATERAL (
        SELECT CASE
                   WHEN w.avg_cost IS NULL AND f.costed_in = 0 THEN NULL
                   WHEN w.qty + f.in_qty > 0
                       THEN round((w.value + f.in_value) / (w.qty + f.in_qty), 6)
                   ELSE w.avg_cost
               END AS avg_cost
    ) AS a
    CROSS JOIN LATERAL (
        SELECT SUM(m.quantity) AS out_qty,
               SUM(CASE WHEN a.avg_cost IS NULL THEN 0
                        ELSE round(m.quantity * a.avg_cost, :scale) END) AS out_value
        FROM inventory_movements m
        WHERE m.branch_id = w.branch_id
          AND m.item_id = w.item_id
          AND m.lot_key = w.lot_key
          AND m.period_id = f.period_id
          AND m.direction = -1
          AND NOT m.is_custodial
    ) AS x
)
UPDATE inventory_movements m
SET unit_cost = w.avg_cost,
    amount = CASE WHEN w.avg_cost IS NULL THEN NULL ELSE round(m.quantity * w.avg_cost, :scale) END,
    cost_state = CASE WHEN w.avg_cost IS NULL THEN 0 ELSE 1 END
FROM walk w
WHERE w.rn > 0
  AND m.branch_id = w.branch_id
  AND m.item_id = w.item_id
  AND m.lot_key = w.lot_key
  AND m.period_id = w.period_id
  AND m.direction = -1
  AND NOT m.is_custodial
  AND (
      m.unit_cost IS DISTINCT FROM w.avg_cost
      OR m.amount IS DISTINCT FROM
         CASE WHEN w.avg_cost IS NULL THEN NULL ELSE round(m.quantity * w.avg_cost, :scale) END
      OR m.cost_state <> CASE WHEN w.avg_cost IS NULL THEN 0 ELSE 1 END
  )
RETURNING m.id, m.voucher_id, m.branch_id, m.warehouse_id, m.item_id, m.lot_key
