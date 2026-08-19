-- Snapshot khớp tổng hợp lại từ nguồn sự thật: cột phát sinh của
-- `account_balances` phải bằng SUM từ `gl_postings` cho từng
-- (kỳ, sổ, chi nhánh, TK, tiền tệ) — và không bên nào có dòng bên kia thiếu.
--
-- FULL JOIN chứ không INNER: một dòng snapshot "mồ côi" (chứng từ đã bỏ ghi
-- sổ nhưng DELETE snapshot bị vòng qua) và một khóa có phát sinh mà snapshot
-- chưa từng tính đều là lệch — INNER JOIN mù cả hai. Lát còn dấu bẩn trong
-- hàng đợi được loại vì snapshot của chúng được phép cũ (xem
-- `trial_balance_balanced.sql`); cột dư đầu/dư cuối lăn theo kỳ nên sai ở
-- chúng luôn kéo theo sai phát sinh ở một kỳ nào đó trong dải bẩn.
WITH posted AS (
    SELECT p.period_id, p.ledger, p.branch_id, p.account_id, p.currency_code,
           SUM(p.debit)  AS period_debit,
           SUM(p.credit) AS period_credit
    FROM gl_postings p
    WHERE p.branch_id = :branch_id
    GROUP BY p.period_id, p.ledger, p.branch_id, p.account_id, p.currency_code
),
snap AS (
    SELECT b.period_id, b.ledger, b.branch_id, b.account_id, b.currency_code,
           b.period_debit, b.period_credit
    FROM account_balances b
    WHERE b.branch_id = :branch_id
)
SELECT COALESCE(s.period_id, p.period_id)         AS period_id,
       COALESCE(s.ledger, p.ledger)               AS ledger,
       COALESCE(s.branch_id, p.branch_id)         AS branch_id,
       COALESCE(s.account_id, p.account_id)       AS account_id,
       COALESCE(s.currency_code, p.currency_code) AS currency_code,
       s.period_debit                             AS snapshot_debit,
       s.period_credit                            AS snapshot_credit,
       COALESCE(p.period_debit, 0)                AS posted_debit,
       COALESCE(p.period_credit, 0)               AS posted_credit
FROM snap s
FULL JOIN posted p
  ON  p.period_id = s.period_id AND p.ledger = s.ledger
  AND p.branch_id = s.branch_id AND p.account_id = s.account_id
  AND p.currency_code = s.currency_code
WHERE (s.period_id IS NULL
       OR p.period_id IS NOT NULL AND (s.period_debit  <> p.period_debit
                                    OR s.period_credit <> p.period_credit)
       OR p.period_id IS NULL AND (s.period_debit <> 0 OR s.period_credit <> 0))
  AND NOT EXISTS (
        SELECT 1
        FROM balance_recalc_queue q
        JOIN accounting_periods fp ON fp.id = q.from_period_id
        JOIN accounting_periods bp ON bp.id = COALESCE(s.period_id, p.period_id)
        WHERE q.ledger = COALESCE(s.ledger, p.ledger)
          AND q.branch_id = COALESCE(s.branch_id, p.branch_id)
          AND fp.fiscal_year_id = bp.fiscal_year_id
          AND fp.period_no <= bp.period_no
  )
