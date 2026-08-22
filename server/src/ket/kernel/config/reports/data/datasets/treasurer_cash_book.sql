-- Dataset `treasurer_cash_book`: Sổ quỹ tiền mặt (S07-DN) — sổ của THỦ QUỸ.
--
-- Khác `money_account_ledger` (Sổ kế toán chi tiết quỹ tiền mặt, S07a-DN) ở
-- chỗ căn bản: nguồn là `treasurer_cash_book` — thứ thủ quỹ thực thu/thực chi
-- và ghi sổ quỹ — chứ không phải `gl_postings`. Hai sổ CỐ TÌNH tách nguồn:
-- đối chiếu chúng với nhau là chính việc mà BR-WHK-03 đòi (báo cáo
-- `treasurer_ledger_diff`), nên gộp nguồn sẽ làm chênh lệch biến mất khỏi
-- chính báo cáo sinh ra để tìm nó.
--
-- Ngày trên sổ này là `book_date` (ngày thủ quỹ ghi sổ quỹ), không phải
-- `posting_date`: BR-WHK-05 cho phép ghi sổ quỹ sau ngày hạch toán, và sổ quỹ
-- phải đọc theo trình tự tiền THỰC vào/ra két.
--
-- `Tồn` = lũy kế trong khoảng xem + tồn đầu kỳ (tổng phát sinh sổ quỹ trước
-- :from_date). Không có `opening_balances` cho sổ quỹ thủ quỹ — nó là sổ tay
-- của một vai trò, không phải một tài khoản kế toán — nên tồn đầu kỳ chỉ cộng
-- được từ chính bảng này.
--
-- Lọc chi nhánh **bên trong** (`supports_branch = false`): tồn lũy kế phải
-- tính trên đúng tập dòng hiển thị, lọc ở lớp bọc ngoài làm số tồn nhảy. Sổ
-- quỹ không thuộc hệ thống sổ tài chính/quản trị nào (tiền mặt là tiền mặt) —
-- `supports_ledger = false`, tham số sổ không đi vào câu này (đừng viết tên
-- placeholder đó ra ngay cả trong chú thích: `assert_placeholders_allowed`
-- quét cả phần chú thích và sẽ coi đó là placeholder ngoài `allowed_params`).
--
-- Tham số: :from_date, :to_date, :branch_ids, :cash_account_code
WITH scoped AS (
    SELECT b.book_date,
           b.voucher_id,
           b.receipt_amount,
           b.payment_amount,
           coa.code AS account_code,
           coa.name AS account_name
    FROM treasurer_cash_book b
    JOIN chart_of_accounts coa ON coa.id = b.cash_account_id
    WHERE (CAST(:branch_ids AS INTEGER[]) IS NULL OR b.branch_id = ANY(:branch_ids))
      AND (CAST(:cash_account_code AS TEXT) IS NULL OR coa.code = :cash_account_code)
),
opening AS (
    SELECT account_code,
           MIN(account_name)                             AS account_name,
           SUM(receipt_amount) - SUM(payment_amount)     AS net
    FROM scoped
    WHERE book_date < :from_date
    GROUP BY account_code
),
lines AS (
    SELECT s.account_code,
           s.account_name,
           s.book_date,
           v.document_date,
           -- Mẫu S07-DN tách SỐ HIỆU chứng từ thành hai cột Thu/Chi; một dòng
           -- sổ quỹ chỉ mang một chiều (phiếu net-0 không vào hàng đợi — 6C
           -- H-2) nên đúng một trong hai cột có giá trị.
           CASE WHEN s.receipt_amount > 0 THEN v.voucher_no END AS receipt_no,
           CASE WHEN s.payment_amount > 0 THEN v.voucher_no END AS payment_no,
           v.description,
           s.receipt_amount,
           s.payment_amount,
           SUM(s.receipt_amount - s.payment_amount) OVER w AS running_net,
           ROW_NUMBER() OVER w                             AS rn
    FROM scoped s
    JOIN vouchers v ON v.id = s.voucher_id
    WHERE s.book_date >= :from_date
      AND s.book_date <= :to_date
    -- Định danh chứng từ trong khóa sắp (bài học H1 lát 5D): đánh số theo chi
    -- nhánh làm "hai chứng từ trùng số cùng ngày" thành chuyện thường ngày.
    WINDOW w AS (
        PARTITION BY s.account_code
        ORDER BY s.book_date, v.voucher_no, v.branch_id, s.voucher_id
    )
),
account_universe AS (
    SELECT u.account_code, MIN(u.account_name) AS account_name
    FROM (
        SELECT account_code, account_name FROM opening WHERE net <> 0
        UNION ALL
        SELECT DISTINCT account_code, account_name FROM lines
    ) u
    GROUP BY u.account_code
)
SELECT au.account_code,
       au.account_name,
       0                             AS sort_seq,
       CAST(0 AS BIGINT)             AS rn,
       CAST(NULL AS DATE)            AS book_date,
       CAST(NULL AS DATE)            AS document_date,
       CAST(NULL AS TEXT)            AS receipt_no,
       CAST(NULL AS TEXT)            AS payment_no,
       'Số tồn đầu kỳ'               AS description,
       CAST(NULL AS NUMERIC)         AS receipt_amount,
       CAST(NULL AS NUMERIC)         AS payment_amount,
       COALESCE(o.net, 0)            AS balance
FROM account_universe au
LEFT JOIN opening o ON o.account_code = au.account_code
UNION ALL
SELECT l.account_code,
       l.account_name,
       1                             AS sort_seq,
       l.rn,
       l.book_date,
       l.document_date,
       l.receipt_no,
       l.payment_no,
       l.description,
       l.receipt_amount,
       l.payment_amount,
       COALESCE(o.net, 0) + l.running_net AS balance
FROM lines l
LEFT JOIN opening o ON o.account_code = l.account_code
