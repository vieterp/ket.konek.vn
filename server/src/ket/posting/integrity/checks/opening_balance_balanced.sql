-- BR-OPB-01: tổng dư Nợ đầu năm = tổng dư Có, từng (năm, sổ, chi nhánh).
--
-- 4C ghi lệch là CẢNH BÁO lúc nhập (đúng chữ FR-OPB-006 — người ta nhập
-- từng nhóm, giữa chừng lệch là bình thường); chỗ chặn cứng là cổng khóa kỳ
-- đầu năm (`lock_service`). Check này là con mắt thứ ba chạy định kỳ: lệch
-- còn nằm đây nghĩa là năm chưa khóa được kỳ nào — sửa sớm rẻ hơn sửa vào
-- ngày 31/12.
SELECT ob.fiscal_year_id AS fiscal_year_id,
       ob.ledger         AS ledger,
       ob.branch_id      AS branch_id,
       SUM(ob.debit)     AS total_debit,
       SUM(ob.credit)    AS total_credit,
       SUM(ob.debit) - SUM(ob.credit) AS imbalance
FROM opening_balances ob
WHERE ob.branch_id = :branch_id
GROUP BY ob.fiscal_year_id, ob.ledger, ob.branch_id
HAVING SUM(ob.debit) <> SUM(ob.credit)
