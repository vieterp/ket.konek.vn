-- FR-STK-008 — số chứng từ kho còn movement chưa tính giá (tổng thật cho danh
-- sách `uncosted_vouchers.sql`). Cùng điều kiện lọc.
--
-- Tham số: :branch_id, :date_from, :date_to.
SELECT COUNT(DISTINCT m.voucher_id) AS vouchers
FROM inventory_movements m
WHERE m.branch_id = :branch_id
  AND m.voucher_id IS NOT NULL
  AND m.cost_state <> 1
  AND NOT m.is_custodial
  AND (CAST(:date_from AS DATE) IS NULL OR m.posting_date >= CAST(:date_from AS DATE))
  AND (CAST(:date_to AS DATE) IS NULL OR m.posting_date <= CAST(:date_to AS DATE))
