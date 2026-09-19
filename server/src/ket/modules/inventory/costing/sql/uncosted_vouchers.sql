-- FR-STK-008 — chứng từ kho còn movement CHƯA TÍNH GIÁ (`cost_state <> 1`:
-- chờ giá hoặc cần tính lại), mỗi chứng từ một dòng kèm số dòng chưa giá, sớm
-- nhất trước, cắt ở :limit; tổng thật do `uncosted_count.sql`.
--
-- Lớp đầu kỳ (`voucher_id` NULL) luôn COSTED nên không lọt; hàng giữ hộ không
-- có giá theo định nghĩa.
--
-- Tham số: :branch_id, :date_from, :date_to (NULL = không chặn), :limit.
SELECT m.voucher_id, v.voucher_no, v.document_type, v.posting_date,
       COUNT(*) AS movements
FROM inventory_movements m
JOIN vouchers v ON v.id = m.voucher_id
WHERE m.branch_id = :branch_id
  AND m.cost_state <> 1
  AND NOT m.is_custodial
  AND (CAST(:date_from AS DATE) IS NULL OR m.posting_date >= CAST(:date_from AS DATE))
  AND (CAST(:date_to AS DATE) IS NULL OR m.posting_date <= CAST(:date_to AS DATE))
GROUP BY m.voucher_id, v.voucher_no, v.document_type, v.posting_date
ORDER BY v.posting_date, v.voucher_no
LIMIT :limit
