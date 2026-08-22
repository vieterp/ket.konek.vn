-- Dataset `treasurer_ledger_diff`: Chênh lệch sổ quỹ ↔ sổ kế toán (FR-WHK-023,
-- BR-WHK-03) — món nợ lát 6C bàn giao cho 6E.
--
-- Check toàn vẹn thứ 8 (`posting/integrity/checks/treasurer_book_matches_
-- ledger.sql`) đã CANH những trạng thái không-bao-giờ-hợp-lệ và kêu khi hệ
-- thống tự mâu thuẫn. Báo cáo này là thứ khác: nó cho NGƯỜI đọc mọi chênh
-- lệch, **kể cả chênh lệch hợp lệ về nghiệp vụ** — phiếu kế toán đã ghi sổ mà
-- thủ quỹ chưa ghi sổ quỹ (BR-WHK-01, trạng thái chờ) là tiền chưa thực vào
-- két: hoàn toàn đúng luật, và chính là thứ kế toán trưởng cần nhìn cuối ngày.
-- Vì vậy ở đây KHÔNG lọc theo tập điều kiện của check; lọc là "chênh lệch ≠ 0
-- hoặc một bên vắng mặt".
--
-- Đơn vị so sánh = MỘT phiếu quỹ. Hai vế:
--   * `ledger_net` — phần chạm TK quỹ của phiếu trên **sổ tài chính**
--     (`gl_postings`, ledger 0). Lọc `ledger` cứng chứ không nhận tham số:
--     sổ quỹ thủ quỹ là một sổ tay duy nhất, không có bản sổ quản trị của nó,
--     nên so nó với sổ quản trị là so hai thứ không cùng định nghĩa.
--   * `book_net` — thu trừ chi trên `treasurer_cash_book`.
--
-- Trạng thái: vouchers.status 2 = Đã ghi sổ; treasurer_status 0 chờ / 1 đã ghi
-- sổ quỹ / 2 không áp dụng (phân hệ tắt).
--
-- Lọc chi nhánh **bên trong** (`supports_branch = false`): hai vế phải cùng
-- phạm vi chi nhánh, nếu không một phiếu sẽ hiện ra "lệch" chỉ vì một vế bị
-- lớp bọc ngoài cắt mất. `supports_ledger = false` vì sổ đã cố định như trên.
--
-- Tham số: :from_date, :to_date, :branch_ids, :cash_account_code
WITH cash_ledger AS (
    SELECT cv.id                AS voucher_id,
           v.voucher_no,
           v.posting_date,
           v.description,
           v.status             AS voucher_status,
           cv.treasurer_status,
           cv.cash_account_id,
           coa.code             AS account_code,
           coa.name             AS account_name,
           COALESCE(SUM(p.debit - p.credit)
                    FILTER (WHERE p.account_id = cv.cash_account_id), 0) AS ledger_net
    FROM cash_vouchers cv
    JOIN vouchers v ON v.id = cv.id
    JOIN chart_of_accounts coa ON coa.id = cv.cash_account_id
    LEFT JOIN gl_postings p ON p.voucher_id = cv.id AND p.ledger = 0
    WHERE (CAST(:branch_ids AS INTEGER[]) IS NULL OR v.branch_id = ANY(:branch_ids))
      AND (CAST(:cash_account_code AS TEXT) IS NULL OR coa.code = :cash_account_code)
      AND v.posting_date >= :from_date
      AND v.posting_date <= :to_date
    GROUP BY cv.id, v.voucher_no, v.posting_date, v.description, v.status,
             cv.treasurer_status, cv.cash_account_id, coa.code, coa.name
),
book AS (
    SELECT t.voucher_id,
           t.cash_account_id,
           MIN(t.book_date)                                   AS book_date,
           SUM(t.receipt_amount) - SUM(t.payment_amount)      AS book_net
    FROM treasurer_cash_book t
    JOIN chart_of_accounts coa ON coa.id = t.cash_account_id
    WHERE (CAST(:branch_ids AS INTEGER[]) IS NULL OR t.branch_id = ANY(:branch_ids))
      AND (CAST(:cash_account_code AS TEXT) IS NULL OR coa.code = :cash_account_code)
      AND t.book_date >= :from_date
      AND t.book_date <= :to_date
    GROUP BY t.voucher_id, t.cash_account_id
)
SELECT COALESCE(c.account_code, bcoa.code)   AS account_code,
       COALESCE(c.account_name, bcoa.name)   AS account_name,
       c.voucher_no,
       c.posting_date,
       b.book_date,
       c.description,
       COALESCE(c.ledger_net, 0)             AS ledger_net,
       COALESCE(b.book_net, 0)               AS book_net,
       COALESCE(c.ledger_net, 0) - COALESCE(b.book_net, 0) AS difference,
       CASE
           WHEN b.voucher_id IS NULL AND c.treasurer_status = 0
               THEN 'Kế toán đã ghi sổ, thủ quỹ chưa ghi sổ quỹ'
           WHEN b.voucher_id IS NULL
               THEN 'Chỉ có trên sổ kế toán'
           WHEN c.voucher_id IS NULL
               THEN 'Chỉ có trên sổ quỹ'
           WHEN c.voucher_status <> 2
               THEN 'Có trên sổ quỹ nhưng phiếu chưa/không còn ghi sổ kế toán'
           ELSE 'Lệch số tiền'
       END                                   AS reason
FROM cash_ledger c
FULL JOIN book b
       ON b.voucher_id = c.voucher_id
      AND b.cash_account_id = c.cash_account_id
LEFT JOIN chart_of_accounts bcoa ON bcoa.id = b.cash_account_id
-- Chỉ dòng có chuyện: lệch số, hoặc một bên vắng mặt. Phiếu khớp trọn vẹn
-- không thuộc báo cáo chênh lệch.
WHERE COALESCE(c.ledger_net, 0) <> COALESCE(b.book_net, 0)
   OR c.voucher_id IS NULL
   OR b.voucher_id IS NULL
