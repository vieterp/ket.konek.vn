-- Dataset `gl_ledger`: dòng phát sinh chi tiết theo tài khoản — nguồn của Sổ
-- Cái và (5D) các sổ chi tiết tài khoản (RT-18: report engine phải chứng minh
-- được bằng chính bộ sổ).
--
-- BR-RPT-01 thỏa sẵn: `gl_postings` chỉ chứa chứng từ ĐÃ ghi sổ. RLS lọc chi
-- nhánh trước khi câu lệnh này thấy dữ liệu; lớp bọc phạm vi của engine
-- (`executor.py`) chỉ là phòng thủ lớp hai (BR-RPT-04/05).
--
-- Gộp/nhóm theo **số hiệu TK** (`coa.code`), không theo `account_id` — sau một
-- lần đổi gói cấu hình, phát sinh cũ trỏ vào dòng TK của gói cũ nhưng cùng số
-- hiệu (trục gộp theo số hiệu, LD-17/4F).
--
-- `branch_id` + `ledger` có mặt trong SELECT là hợp đồng với lớp bọc phạm vi —
-- probe `LIMIT 0` của seed chạy chính câu SQL đã bọc nên thiếu cột là cấp
-- dữ liệu kế toán thất bại ngay (không có kiểm regex tĩnh nào ở đây).
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids (lớp bọc), :account_code
SELECT
    p.branch_id,
    p.ledger,
    coa.code                               AS account_code,
    coa.name                               AS account_name,
    p.posting_date,
    v.voucher_no,
    v.document_date,
    v.document_type,
    COALESCE(p.description, v.description) AS description,
    corr.code                              AS corresponding_account,
    p.debit,
    p.credit,
    p.line_no
FROM gl_postings p
JOIN vouchers v ON v.id = p.voucher_id
JOIN chart_of_accounts coa ON coa.id = p.account_id
LEFT JOIN chart_of_accounts corr ON corr.id = p.corresponding_account_id
WHERE p.ledger = :ledger
  AND p.posting_date >= :from_date
  AND p.posting_date <= :to_date
  AND (CAST(:account_code AS TEXT) IS NULL OR coa.code = :account_code)
