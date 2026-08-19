-- BR-GLE-01: mỗi chứng từ, từng sổ, tổng Nợ = tổng Có.
--
-- Lệch ở đây nghĩa là có đường ghi vòng qua PostingService (validator
-- `balanced` chặn từ lúc ghi sổ) hoặc dữ liệu bị sửa/xóa thẳng bằng SQL —
-- đúng loại hỏng mà FR-NFR-007 sinh ra để chỉ mặt. Không tự sửa, chỉ trả
-- dòng chênh kèm chứng từ để người dùng lần tới (cột `voucher_id`).
--
-- RLS lọc chi nhánh sẵn; `:branch_id` là phòng thủ lớp hai (kế hoạch §RLS).
SELECT v.id::text        AS voucher_id,
       v.voucher_no      AS voucher_no,
       v.document_type   AS document_type,
       p.ledger          AS ledger,
       SUM(p.debit)      AS total_debit,
       SUM(p.credit)     AS total_credit,
       SUM(p.debit) - SUM(p.credit) AS imbalance
FROM gl_postings p
JOIN vouchers v ON v.id = p.voucher_id
WHERE p.branch_id = :branch_id
GROUP BY v.id, v.voucher_no, v.document_type, p.ledger
HAVING SUM(p.debit) <> SUM(p.credit)
