-- Bảng cân đối tài khoản tính THẲNG từ `gl_postings` + `opening_balances` —
-- đường "chậm nhưng luôn đúng" khi kỳ còn dấu bẩn trong `balance_recalc_queue`
-- (phase-04 §Snapshot: báo cáo không được tin snapshot của kỳ bẩn).
--
-- Cùng phép toán với `recalc_period.sql` (net theo (TK, tiền tệ) rồi tách hai
-- bên bằng GREATEST) — hai đường phải cho cùng một con số, test canh điều đó.
--
-- Tham số:
--   :ledger, :fiscal_year_id, :year_start, :period_start, :period_end
--   :branch_id — NULL = mọi chi nhánh trong phạm vi RLS của người gọi
--
-- Ranh giới kỳ đi bằng NGÀY chứ không danh sách `period_id`: `posting_date`
-- quyết định `period_id` lúc ghi sổ (`resolve_period`), nên hai cách tương
-- đương — CHỪNG NÀO mọi kỳ còn được gán theo ngày. **Kỳ 13 điều chỉnh (phase
-- 10a) phá tương đương này**: nó trùng khoảng ngày kỳ 12 nhưng mang
-- `period_id` riêng, nên khi kỳ 13 ra đời file này PHẢI đổi sang lọc theo
-- `period_id` (join `accounting_periods`) — review 4B, phát hiện H1.
WITH opening_source AS (
    SELECT account_id, currency_code, SUM(debit) - SUM(credit) AS net
    FROM opening_balances
    WHERE fiscal_year_id = :fiscal_year_id
      AND ledger = :ledger
      AND (CAST(:branch_id AS INTEGER) IS NULL OR branch_id = :branch_id)
    GROUP BY account_id, currency_code
    UNION ALL
    SELECT account_id, currency_code, SUM(debit) - SUM(credit)
    FROM gl_postings
    WHERE ledger = :ledger
      AND (CAST(:branch_id AS INTEGER) IS NULL OR branch_id = :branch_id)
      AND posting_date >= :year_start
      AND posting_date < :period_start
    GROUP BY account_id, currency_code
),
opening AS (
    SELECT account_id, currency_code, SUM(net) AS net
    FROM opening_source
    GROUP BY account_id, currency_code
),
movement AS (
    SELECT account_id, currency_code,
           SUM(debit)  AS period_debit,
           SUM(credit) AS period_credit
    FROM gl_postings
    WHERE ledger = :ledger
      AND (CAST(:branch_id AS INTEGER) IS NULL OR branch_id = :branch_id)
      AND posting_date >= :period_start
      AND posting_date <= :period_end
    GROUP BY account_id, currency_code
),
merged AS (
    SELECT account_id,
           COALESCE(o.net, 0)           AS net,
           COALESCE(m.period_debit, 0)  AS period_debit,
           COALESCE(m.period_credit, 0) AS period_credit
    FROM opening o
    FULL OUTER JOIN movement m USING (account_id, currency_code)
),
-- Gộp theo SỐ HIỆU TK, không theo `account_id` (quyết định trục gộp 2026-08-20).
-- Tài khoản thuộc **gói cấu hình**, nên cùng một TK "112" ở hai gói là hai
-- `account_id` khác nhau; gộp theo id sẽ tách đôi một tài khoản ngay khi
-- dataset đổi/nâng cấp gói giữa chừng, còn BCTC (`statement_balances.sql`) thì
-- gộp theo `code` — hai báo cáo lệch nhau đúng ở chỗ khó truy nhất. `code` là
-- trục đúng cho mọi báo cáo trình bày cho người đọc.
by_code AS (
    SELECT a.code AS account_code, g.net, g.period_debit, g.period_credit
    FROM merged g
    JOIN chart_of_accounts a ON a.id = g.account_id
)
-- Tách hai bên Ở MỨC TÀI KHOẢN (sau khi cộng mọi tiền tệ): tách theo từng
-- tiền tệ rồi cộng sẽ cho một TK "vừa dư Nợ vừa dư Có" chỉ vì nó cầm hai loại
-- tiền — bảng cân đối TK trình bày số quy đổi VND, mỗi TK một bên dư.
SELECT account_code,
       GREATEST(SUM(net), 0)                                   AS opening_debit,
       GREATEST(-SUM(net), 0)                                  AS opening_credit,
       SUM(period_debit)                                       AS period_debit,
       SUM(period_credit)                                      AS period_credit,
       GREATEST(SUM(net + period_debit - period_credit), 0)    AS closing_debit,
       GREATEST(-SUM(net + period_debit - period_credit), 0)   AS closing_credit
FROM by_code
GROUP BY account_code
HAVING NOT (
    SUM(net) = 0
    AND SUM(period_debit) = 0
    AND SUM(period_credit) = 0
)
