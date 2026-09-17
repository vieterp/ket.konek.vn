-- Mảnh SQL dùng chung — **không phải một dataset**. Nhúng bằng
-- `-- #include: settlement_rows.sql` (xem `loader._expand_includes`).
--
-- Mọi dòng đối trừ công nợ của hệ, ở mức DÒNG: năm phân hệ ghi lượt đối trừ, cả
-- năm mang đúng một bộ cột, nên khối này là **một** nguồn chứ không phải năm.
-- Thêm một phân hệ đối trừ thứ sáu là thêm một nhánh Ở ĐÂY, và mọi báo cáo công
-- nợ thấy nó cùng lúc — đó là cả lý do khối này rời khỏi tệp dataset.
--
-- Không lọc gì: phép lọc "chỉ chứng từ đã ghi sổ" và phép cắt theo ngày thuộc về
-- tệp gọi, vì hai câu hỏi khác nhau cắt khác nhau (số dư công nợ cắt theo mốc
-- chốt; lịch sử thanh toán cắt theo cả khoảng kỳ).
SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
  FROM cash_settlements
UNION ALL
SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
  FROM bank_settlements
UNION ALL
SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
  FROM purchase_settlements
UNION ALL
SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
  FROM sales_settlements
UNION ALL
SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
  FROM gl_journal_settlements
