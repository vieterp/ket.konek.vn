-- Vế NHẬP các linh kiện của phiếu THÁO DỠ (kind 4, lát 8C-2) chia giá trị vế
-- XUẤT thành phẩm theo `allocation_ratio` của dòng phiếu — SRS 09 §2.3 "nhập
-- các linh kiện theo tỷ lệ phân bổ giá trị". Chạy sau câu phương pháp ở MỖI
-- vòng, cùng khuôn `assembly_in_legs.sql` / `transfer_in_legs.sql`.
--
-- Làm tròn: mỗi dòng `round(total × r_i / Σr, :scale)`; dòng có TỶ LỆ LỚN NHẤT
-- (bằng nhau → `line_no` lớn nhất) nhận `total − Σ(các dòng khác)` để Σ linh
-- kiện = total tới từng đồng (sổ kho khớp sổ cái, BR-STK-03). Dồn vào dòng lớn
-- nhất chứ không dòng cuối: phần dư tối đa ±(n − 1)/2 đơn vị tiền, dòng lớn
-- nhất luôn ≥ total/n nên không bao giờ âm (review 8C-2 L-1 — tỷ lệ tí hon ở
-- dòng cuối từng cho ra âm). Thành phẩm chưa có giá → mọi linh kiện về PENDING.
--
-- Tham số: :branch_id, :year_start, :year_end, :scale.
WITH product AS (
    SELECT o.voucher_id, o.amount AS total, o.unit_cost AS product_cost
    FROM inventory_vouchers b
    JOIN inventory_movements o ON o.voucher_id = b.id
    WHERE b.kind = 4
      AND o.direction = -1
      AND o.branch_id = :branch_id
      AND o.posting_date BETWEEN :year_start AND :year_end
),
parts AS (
    SELECT i.id, i.voucher_id, i.quantity, p.total, p.product_cost,
           round(p.total * l.allocation_ratio
                 / SUM(l.allocation_ratio) OVER (PARTITION BY i.voucher_id), :scale) AS rounded,
           ROW_NUMBER() OVER (
               PARTITION BY i.voucher_id ORDER BY l.allocation_ratio DESC, l.line_no DESC
           ) AS rn
    FROM product p
    JOIN inventory_movements i ON i.voucher_id = p.voucher_id AND i.direction = 1
    JOIN inventory_voucher_lines l ON l.id = i.line_id
    WHERE NOT i.is_custodial
),
alloc AS (
    SELECT id, quantity, product_cost,
           CASE WHEN product_cost IS NULL THEN NULL
                WHEN rn = 1 THEN total - (SUM(rounded) OVER (PARTITION BY voucher_id) - rounded)
                ELSE rounded END AS new_amount
    FROM parts
)
UPDATE inventory_movements i
SET amount = a.new_amount,
    unit_cost = CASE WHEN a.new_amount IS NULL THEN NULL
                     ELSE round(a.new_amount / a.quantity, 6) END,
    cost_state = CASE WHEN a.new_amount IS NULL THEN 0 ELSE 2 END
FROM alloc a
WHERE a.id = i.id
  AND (
      i.amount IS DISTINCT FROM a.new_amount
      OR i.unit_cost IS DISTINCT FROM
         CASE WHEN a.new_amount IS NULL THEN NULL ELSE round(a.new_amount / a.quantity, 6) END
  )
RETURNING i.id, i.voucher_id, i.branch_id, i.warehouse_id, i.item_id, i.lot_key
