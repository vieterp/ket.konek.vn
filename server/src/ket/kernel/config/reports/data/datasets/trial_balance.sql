-- Dataset `trial_balance`: Bảng cân đối số phát sinh (S06-DN) / Bảng cân đối
-- tài khoản (F01-DNN) — mỗi tài khoản một dòng: dư đầu kỳ, phát sinh trong
-- kỳ, dư cuối kỳ, mỗi bộ tách hai bên Nợ/Có.
--
-- CÙNG PHÉP TOÁN với `posting/balances/sql/trial_balance_direct.sql`
-- (BR-RPT-02: bản in và màn hình `GET /ledger/trial-balance` phải cho cùng
-- con số trên cùng dữ liệu — test tích hợp canh điều đó): net theo tài khoản
-- rồi tách hai bên bằng GREATEST ở mức TÀI KHOẢN sau khi cộng mọi tiền tệ;
-- gộp theo SỐ HIỆU TK, không theo `account_id` (trục gộp LD-17/4F).
--
-- Khác biệt duy nhất: năm tài chính suy từ :from_date (report engine nhận
-- from/to, không nhận fiscal_year_id). Khoảng vắt qua hai năm tài chính sẽ
-- lấy dư đầu của năm chứa :from_date — cùng giới hạn với mọi báo cáo theo
-- khoảng ngày; kỳ 13 điều chỉnh (10a) là caveat đã ghi ở
-- trial_balance_direct.sql.
--
-- Lọc chi nhánh + sổ bên trong (`supports_branch/ledger = false`): dòng kết
-- quả là số GỘP theo tài khoản, không mang `branch_id` để lớp bọc ngoài lọc
-- được nữa. RLS vẫn là cô lập thật (RT-04).
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids
WITH fy AS (
    SELECT id, start_date
    FROM fiscal_years
    WHERE :from_date >= start_date AND :from_date <= end_date
),
opening_source AS (
    SELECT ob.account_id, SUM(ob.debit) - SUM(ob.credit) AS net
    FROM opening_balances ob
    JOIN fy ON ob.fiscal_year_id = fy.id
    WHERE ob.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR ob.branch_id = ANY(:branch_ids))
    GROUP BY ob.account_id
    UNION ALL
    SELECT p.account_id, SUM(p.debit) - SUM(p.credit)
    FROM gl_postings p
    JOIN fy ON TRUE
    WHERE p.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
      AND p.posting_date >= fy.start_date
      AND p.posting_date < :from_date
    GROUP BY p.account_id
),
movement AS (
    SELECT p.account_id,
           SUM(p.debit)  AS period_debit,
           SUM(p.credit) AS period_credit
    FROM gl_postings p
    WHERE p.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
      AND p.posting_date >= :from_date
      AND p.posting_date <= :to_date
    GROUP BY p.account_id
),
merged AS (
    SELECT account_id,
           COALESCE(o.net, 0)          AS net,
           COALESCE(m.period_debit, 0) AS period_debit,
           COALESCE(m.period_credit, 0) AS period_credit
    FROM (
        SELECT account_id, SUM(net) AS net
        FROM opening_source
        GROUP BY account_id
    ) o
    FULL OUTER JOIN movement m USING (account_id)
),
by_code AS (
    SELECT coa.code AS account_code, MIN(coa.name) AS account_name,
           SUM(g.net) AS net,
           SUM(g.period_debit) AS period_debit,
           SUM(g.period_credit) AS period_credit
    FROM merged g
    JOIN chart_of_accounts coa ON coa.id = g.account_id
    GROUP BY coa.code
)
SELECT account_code,
       account_name,
       GREATEST(net, 0)                                   AS opening_debit,
       GREATEST(-net, 0)                                  AS opening_credit,
       period_debit,
       period_credit,
       GREATEST(net + period_debit - period_credit, 0)    AS closing_debit,
       GREATEST(-(net + period_debit - period_credit), 0) AS closing_credit
FROM by_code
WHERE NOT (net = 0 AND period_debit = 0 AND period_credit = 0)
