-- Sổ quỹ của thủ quỹ khớp sổ kế toán TK tiền mặt (BR-WHK-03, FR-WHK-023 — lát 6C).
--
-- Hai sổ song song CÓ THỂ lệch một cách hợp lệ: phiếu đã ghi sổ kế toán mà thủ
-- quỹ chưa ghi sổ quỹ (BR-WHK-01, trạng thái chờ) là chênh lệch NGHIỆP VỤ —
-- việc của báo cáo chênh lệch (lát 6E), không phải của check này. Check này
-- chỉ bắt trạng thái KHÔNG BAO GIỜ hợp lệ:
--
-- * dòng sổ quỹ trỏ vào phiếu không-còn-ghi-sổ (hoặc không phải phiếu quỹ) —
--   đường bỏ ghi sổ phải gỡ dòng sổ quỹ theo (hook `after_unpost`);
-- * dòng sổ quỹ lệch số với phần chạm TK quỹ của chính phiếu đó trên sổ tài
--   chính (`gl_postings`) — hai sổ cùng đơn vị VND quy đổi;
-- * phiếu mang trạng thái "đã ghi sổ quỹ"/"không áp dụng" (đã ghi sổ kế toán,
--   có tiền chạm quỹ) mà không có dòng sổ quỹ nào;
-- * dòng sổ quỹ tồn tại nhưng phiếu vẫn mang trạng thái "chờ" — ai đó ghi sổ
--   vòng qua đường lật trạng thái.
--
-- Trạng thái: vouchers.status 2 = Đã ghi sổ; treasurer_status 0 chờ / 1 đã ghi
-- sổ quỹ / 2 không áp dụng (phân hệ tắt). ledger 0 = sổ tài chính.
WITH cash_ledger AS (
    SELECT cv.id AS voucher_id,
           v.status AS voucher_status,
           cv.treasurer_status,
           COALESCE(SUM(p.debit - p.credit)
                    FILTER (WHERE p.account_id = cv.cash_account_id), 0) AS ledger_net
    FROM cash_vouchers cv
    JOIN vouchers v ON v.id = cv.id
    LEFT JOIN gl_postings p ON p.voucher_id = cv.id AND p.ledger = 0
    WHERE v.branch_id = :branch_id
    GROUP BY cv.id, v.status, cv.treasurer_status
),
book AS (
    SELECT t.voucher_id, t.receipt_amount - t.payment_amount AS book_net
    FROM treasurer_cash_book t
    WHERE t.branch_id = :branch_id
)
SELECT COALESCE(c.voucher_id, b.voucher_id) AS voucher_id,
       c.voucher_status,
       c.treasurer_status,
       b.book_net,
       c.ledger_net
FROM cash_ledger c
FULL JOIN book b ON b.voucher_id = c.voucher_id
WHERE (b.voucher_id IS NOT NULL AND (c.voucher_id IS NULL OR c.voucher_status <> 2))
   OR (b.voucher_id IS NOT NULL AND c.voucher_status = 2 AND b.book_net <> c.ledger_net)
   OR (b.voucher_id IS NOT NULL AND c.treasurer_status = 0)
   OR (b.voucher_id IS NULL AND c.voucher_status = 2
       AND c.treasurer_status IN (1, 2) AND c.ledger_net <> 0)
