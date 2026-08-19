-- BR-GLE-02: bảng cân đối TK cân cả 3 cột (dư đầu, phát sinh, dư cuối) cho
-- mỗi (kỳ, sổ, chi nhánh) — tính trên snapshot `account_balances`.
--
-- Loại các lát (sổ, chi nhánh, kỳ) còn dấu bẩn trong hàng đợi tính lại:
-- snapshot của chúng ĐƯỢC PHÉP cũ (thiết kế ADR-011 — báo cáo hiện "đang chờ
-- tính lại"), nên báo chúng lệch ở đây là báo oan một trạng thái đã có tên.
-- Lát sạch mà vẫn lệch mới là chuyện của check này; khớp từng dòng với
-- `gl_postings` thuộc check `snapshot_matches_postings`.
SELECT b.period_id             AS period_id,
       b.ledger                AS ledger,
       b.branch_id             AS branch_id,
       SUM(b.opening_debit)    AS opening_debit,
       SUM(b.opening_credit)   AS opening_credit,
       SUM(b.period_debit)     AS period_debit,
       SUM(b.period_credit)    AS period_credit,
       SUM(b.closing_debit)    AS closing_debit,
       SUM(b.closing_credit)   AS closing_credit
FROM account_balances b
JOIN accounting_periods bp ON bp.id = b.period_id
WHERE b.branch_id = :branch_id
  AND NOT EXISTS (
        SELECT 1
        FROM balance_recalc_queue q
        JOIN accounting_periods fp ON fp.id = q.from_period_id
        WHERE q.ledger = b.ledger
          AND q.branch_id = b.branch_id
          AND fp.fiscal_year_id = bp.fiscal_year_id
          AND fp.period_no <= bp.period_no
  )
GROUP BY b.period_id, b.ledger, b.branch_id
HAVING SUM(b.opening_debit) <> SUM(b.opening_credit)
    OR SUM(b.period_debit)  <> SUM(b.period_credit)
    OR SUM(b.closing_debit) <> SUM(b.closing_credit)
