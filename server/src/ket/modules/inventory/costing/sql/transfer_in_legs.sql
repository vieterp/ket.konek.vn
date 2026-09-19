-- Vế ĐẾN của phiếu chuyển kho nhận đúng giá vế ĐI (cùng `voucher_id` +
-- `line_id`) — chạy sau câu phương pháp ở MỖI vòng. Vế đi thuộc khóa nguồn,
-- vế đến thuộc khóa đích; vòng này ghi giá sang, vòng sau khóa đích thấy nó.
--
-- Không lọc theo `keys`: khóa đích có thể chưa bẩn (chỉ khóa nguồn có dấu) —
-- ghi vế đến với `cost_state = 2` (STALE) chính là cách kéo khóa đích vào tập
-- `keys` của vòng sau (nhánh `cost_state <> 1`), để các dòng xuất sau nó ở kho
-- đích được tính lại. `finalize.sql` hạ STALE → COSTED sau điểm bất động.
-- Vế đi chưa có giá (NULL) → vế đến về PENDING (ràng buộc `costed_has_unit_cost`).
--
-- Tham số: :branch_id, :year_start, :year_end.
UPDATE inventory_movements i
SET unit_cost = o.unit_cost,
    amount = o.amount,
    cost_state = CASE WHEN o.unit_cost IS NULL THEN 0 ELSE 2 END
FROM inventory_movements o
WHERE o.voucher_id = i.voucher_id
  AND o.line_id = i.line_id
  AND o.direction = -1
  AND i.direction = 1
  AND i.branch_id = :branch_id
  AND i.posting_date BETWEEN :year_start AND :year_end
  AND NOT i.is_custodial
  AND (i.unit_cost IS DISTINCT FROM o.unit_cost OR i.amount IS DISTINCT FROM o.amount)
RETURNING i.id, i.voucher_id, i.branch_id, i.warehouse_id, i.item_id, i.lot_key
