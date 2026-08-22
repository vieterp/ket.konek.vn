-- Dataset `bank_balance_summary`: Bảng kê số dư ngân hàng (`docs/srs/04` §5 #2,
-- FR-BNK-041) — số dư đầu kỳ, phát sinh thu/chi, số dư cuối kỳ theo TỪNG tài
-- khoản ngân hàng và TỪNG loại tiền, hiện đồng thời nguyên tệ và quy đổi.
--
-- Cùng luật quy chủ tài khoản ngân hàng với `money_account_ledger.sql` và
-- `BankDepositMovementSource` (kernel Protocol `DepositMovementSource`, 6D):
-- chủ là `bank_vouchers.bank_account_id`, riêng chuyển tiền nội bộ
-- (`kind = 3`) thì dòng ghi Nợ thuộc tài khoản ĐÍCH. Phát sinh 112x gõ thẳng
-- bằng bút toán GLE không quy được chủ và ở lại nhóm "(chưa gắn)" — không bịa
-- chủ, cùng lối carry-forward 6D.
--
-- Số dư đầu kỳ = dư đầu năm (`opening_balances`, cột `bank_account_id` có từ
-- 0019) + phát sinh từ đầu năm tới TRƯỚC :from_date. Cùng phép toán với
-- `money_account_ledger.sql` để hai báo cáo không cãi nhau (BR-RPT-02).
--
-- Nguyên tệ (`*_fc`) và quy đổi đứng cạnh nhau trên cùng dòng: FR-BNK-041 đòi
-- hiển thị ĐỒNG THỜI, không phải hai báo cáo.
--
-- Lọc chi nhánh + sổ **bên trong** (`supports_branch/ledger = false`): số dư
-- đầu kỳ và phát sinh phải cùng một phạm vi, lọc lệch nhau ở lớp bọc ngoài sẽ
-- cho số dư cuối kỳ không bằng đầu kỳ cộng phát sinh.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids, :bank_account_id
WITH fy AS (
    SELECT id, start_date
    FROM fiscal_years
    WHERE :from_date >= start_date AND :from_date <= end_date
),
postings AS (
    SELECT p.posting_date,
           p.currency_code,
           p.debit,
           p.credit,
           p.debit_fc,
           p.credit_fc,
           CASE
               WHEN bv.id IS NULL THEN NULL
               WHEN bv.kind = 3 AND p.debit > 0 THEN bv.counter_bank_account_id
               ELSE bv.bank_account_id
           END AS bank_account_id
    FROM gl_postings p
    JOIN chart_of_accounts coa ON coa.id = p.account_id
    LEFT JOIN bank_vouchers bv ON bv.id = p.voucher_id
    WHERE p.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
      AND coa.code LIKE '112%'
),
opening AS (
    SELECT bank_account_id, currency_code, SUM(net) AS net, SUM(net_fc) AS net_fc
    FROM (
        SELECT ob.bank_account_id,
               ob.currency_code,
               SUM(ob.debit) - SUM(ob.credit)       AS net,
               SUM(ob.debit_fc) - SUM(ob.credit_fc) AS net_fc
        FROM opening_balances ob
        JOIN chart_of_accounts coa ON coa.id = ob.account_id
        JOIN fy ON ob.fiscal_year_id = fy.id
        WHERE ob.ledger = :ledger
          AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR ob.branch_id = ANY(:branch_ids))
          AND coa.code LIKE '112%'
        GROUP BY ob.bank_account_id, ob.currency_code
        UNION ALL
        SELECT p.bank_account_id,
               p.currency_code,
               SUM(p.debit) - SUM(p.credit),
               SUM(p.debit_fc) - SUM(p.credit_fc)
        FROM postings p
        JOIN fy ON TRUE
        WHERE p.posting_date >= fy.start_date
          AND p.posting_date < :from_date
        GROUP BY p.bank_account_id, p.currency_code
    ) s
    GROUP BY bank_account_id, currency_code
),
movement AS (
    SELECT p.bank_account_id,
           p.currency_code,
           SUM(p.debit)     AS debit,
           SUM(p.credit)    AS credit,
           SUM(p.debit_fc)  AS debit_fc,
           SUM(p.credit_fc) AS credit_fc
    FROM postings p
    WHERE p.posting_date >= :from_date
      AND p.posting_date <= :to_date
    GROUP BY p.bank_account_id, p.currency_code
),
universe AS (
    SELECT bank_account_id, currency_code FROM opening WHERE net <> 0 OR net_fc <> 0
    UNION
    SELECT bank_account_id, currency_code FROM movement
)
SELECT COALESCE(cba.code, '(chưa gắn)')  AS bank_account_code,
       COALESCE(cba.name, '(chưa gắn tài khoản ngân hàng)') AS bank_account_name,
       b.name                            AS bank_name,
       u.currency_code,
       COALESCE(o.net_fc, 0)             AS opening_fc,
       COALESCE(o.net, 0)                AS opening,
       COALESCE(m.debit_fc, 0)           AS receipt_fc,
       COALESCE(m.debit, 0)              AS receipt,
       COALESCE(m.credit_fc, 0)          AS payment_fc,
       COALESCE(m.credit, 0)             AS payment,
       COALESCE(o.net_fc, 0) + COALESCE(m.debit_fc, 0) - COALESCE(m.credit_fc, 0) AS closing_fc,
       COALESCE(o.net, 0) + COALESCE(m.debit, 0) - COALESCE(m.credit, 0)          AS closing
FROM universe u
LEFT JOIN opening o
       ON o.bank_account_id IS NOT DISTINCT FROM u.bank_account_id
      AND o.currency_code = u.currency_code
LEFT JOIN movement m
       ON m.bank_account_id IS NOT DISTINCT FROM u.bank_account_id
      AND m.currency_code = u.currency_code
LEFT JOIN company_bank_accounts cba ON cba.id = u.bank_account_id
LEFT JOIN banks b ON b.id = cba.bank_id
WHERE CAST(:bank_account_id AS INTEGER) IS NULL
   OR u.bank_account_id = :bank_account_id
