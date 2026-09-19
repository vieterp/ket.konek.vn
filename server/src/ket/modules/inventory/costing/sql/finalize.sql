-- Sau điểm bất động: mọi dòng của (chi nhánh, năm) còn `STALE` mà đã có giá →
-- `COSTED`. Dòng xuất đã được câu phương pháp ghi thẳng 0/1; còn lại là dòng
-- NHẬP bị đánh STALE (sắp xếp lại trong ngày đánh cả ngày — giá nhập không
-- đổi) và vế đến chuyển kho (`transfer_in_legs.sql` cố ý để STALE). Dòng chưa
-- có giá giữ nguyên PENDING — `lock_check` và `pending_left` của job nói về nó.
--
-- Tham số: :branch_id, :year_start, :year_end.
UPDATE inventory_movements m
SET cost_state = 1
WHERE m.branch_id = :branch_id
  AND m.posting_date BETWEEN :year_start AND :year_end
  AND m.cost_state = 2
  AND m.unit_cost IS NOT NULL
