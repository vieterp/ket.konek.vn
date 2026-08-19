-- BR-OPB-02: chi tiết đầu kỳ khớp tổng hợp — tổng hóa đơn con
-- (`opening_balance_invoices`, nhóm AR/AP/tạm ứng theo lần) phải bằng đúng
-- số dư của dòng cha.
--
-- Lượt nhập 4C kiểm điều này TRONG tệp; check này bắt trôi lệch về sau: hóa
-- đơn con bị sửa/xóa thẳng bằng SQL, hoặc (từ phase 7) `paid_amount` ăn mòn
-- hóa đơn mà dòng cha không ai đụng. So `amount` chứ không
-- `amount - paid_amount`: dòng cha là SỐ DƯ ĐẦU NĂM — nó cố định theo thời
-- điểm 01/01, còn `paid_amount` là chuyện của năm nay.
--
-- LEFT JOIN + lọc theo `detail_kind` (sửa sau review 4D, M-3): nhóm 2/3/4
-- (`OpeningDetailKind`) do lượt nhập 4C sinh TỪ chính các dòng hóa đơn, nên
-- dòng cha thuộc nhóm này mà không còn hóa đơn con nào nghĩa là chi tiết đã
-- bị xóa sạch — INNER JOIN mù đúng ca đó (0 dòng con = 0 dòng ra). Nhóm khác
-- (0 TK thường…) không có con là bình thường, không thuộc phép so này.
SELECT ob.id             AS opening_balance_id,
       ob.fiscal_year_id AS fiscal_year_id,
       ob.ledger         AS ledger,
       ob.branch_id      AS branch_id,
       ob.account_id     AS account_id,
       ob.detail_kind    AS detail_kind,
       ob.debit + ob.credit AS parent_amount,
       COALESCE(SUM(i.amount), 0) AS invoice_total
FROM opening_balances ob
LEFT JOIN opening_balance_invoices i ON i.opening_balance_id = ob.id
WHERE ob.branch_id = :branch_id
  AND ob.detail_kind IN (2, 3, 4)
GROUP BY ob.id, ob.fiscal_year_id, ob.ledger, ob.branch_id, ob.account_id, ob.detail_kind
HAVING COALESCE(SUM(i.amount), 0) <> ob.debit + ob.credit
