-- Lần NHẬP hàng bán trả lại nhận đúng giá của lần XUẤT mà nó quay về
-- (`source_movement_id` ở chiều nhập, FR-STK-004, lát 8C-1) — chạy sau câu
-- phương pháp và `transfer_in_legs.sql` ở MỖI vòng. Lần xuất thuộc khóa gốc
-- (có thể kho khác), lần nhập thuộc khóa đích; vòng này ghi giá sang, vòng
-- sau khóa đích thấy nó. Tính lại giá bán vì thế TỰ cập nhật giá nhập trả lại,
-- kể cả khác kho hay khác kỳ — đúng chữ "phải cập nhật luôn" của FR-STK-004.
--
-- Không lọc theo `keys`: khóa đích có thể chưa bẩn — ghi với `cost_state = 2`
-- (STALE) chính là cách kéo nó vào tập `keys` của vòng sau (nhánh `cost_state
-- <> 1`); `finalize.sql` hạ STALE → COSTED sau điểm bất động. Lần xuất chưa có
-- giá → lần nhập về PENDING (ràng buộc `costed_has_unit_cost`).
--
-- Thành tiền = round(số lượng × đơn giá 6 lẻ) — trả một phần có thể rơi vài
-- đồng lẻ so với thành tiền lần xuất (cùng ghi nhận L-3 8B).
--
-- Tham số: :branch_id, :year_start, :year_end, :scale.
UPDATE inventory_movements i
SET unit_cost = o.unit_cost,
    amount = CASE WHEN o.unit_cost IS NULL THEN NULL
                  ELSE round(i.quantity * o.unit_cost, :scale) END,
    cost_state = CASE WHEN o.unit_cost IS NULL THEN 0 ELSE 2 END
FROM inventory_movements o
WHERE o.id = i.source_movement_id
  AND o.direction = -1
  AND i.direction = 1
  AND i.branch_id = :branch_id
  AND i.posting_date BETWEEN :year_start AND :year_end
  AND NOT i.is_custodial
  AND (
      i.unit_cost IS DISTINCT FROM o.unit_cost
      OR i.amount IS DISTINCT FROM
         CASE WHEN o.unit_cost IS NULL THEN NULL ELSE round(i.quantity * o.unit_cost, :scale) END
  )
RETURNING i.id, i.voucher_id, i.branch_id, i.warehouse_id, i.item_id, i.lot_key
