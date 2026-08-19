-- Tính toàn bộ dòng snapshot của MỘT (kỳ, sổ, chi nhánh) từ `gl_postings`
-- bằng một câu INSERT … SELECT — không vòng lặp Python trên dòng dữ liệu
-- (ADR-014, LD-14). Chạy SAU câu DELETE cùng khóa trong cùng transaction
-- (xem `recalc.py` — cặp DELETE + INSERT thay cho ON CONFLICT để dòng snapshot
-- mồ côi sau unpost cũng biến mất).
--
-- Tham số:
--   :ledger, :branch_id, :period_id  — khóa của lát snapshot đang tính
--   :prev_period_id                  — kỳ liền trước trong CÙNG năm tài chính;
--                                      NULL = kỳ đầu năm, dư đầu lấy từ
--                                      `opening_balances` thay vì snapshot kỳ trước
--   :fiscal_year_id                  — năm tài chính (chỉ dùng khi kỳ đầu năm)
--
-- Dư đầu/dư cuối là số GỘP theo (TK, tiền tệ): khóa snapshot cố ý không mang
-- đối tác (RT-22), nên TK lưỡng tính hiện dư ròng ở đây — dư hai bên theo đối
-- tượng thuộc `ar_ap_ledger` (phase 7). Hai cột `*_debit_fc` giữ số NGUYÊN TỆ
-- RÒNG có dấu (dương = dư Nợ) — xem docstring `AccountBalance`.
WITH opening AS (
    -- Kỳ đầu năm: gộp từ số dư ban đầu người dùng nhập (FR-OPB-006..009).
    SELECT account_id,
           currency_code,
           SUM(debit) - SUM(credit)       AS net,
           SUM(debit_fc) - SUM(credit_fc) AS net_fc
    FROM opening_balances
    WHERE CAST(:prev_period_id AS INTEGER) IS NULL
      AND fiscal_year_id = :fiscal_year_id
      AND ledger = :ledger
      AND branch_id = :branch_id
    GROUP BY account_id, currency_code
    UNION ALL
    -- Kỳ giữa năm: dư đầu lăn từ dư cuối của snapshot kỳ liền trước —
    -- kỳ trước vừa được tính lại trong CÙNG transaction nên số ở đây luôn mới.
    SELECT account_id,
           currency_code,
           closing_debit - closing_credit,
           opening_debit_fc + period_debit_fc
    FROM account_balances
    WHERE CAST(:prev_period_id AS INTEGER) IS NOT NULL
      AND period_id = :prev_period_id
      AND ledger = :ledger
      AND branch_id = :branch_id
),
movement AS (
    SELECT account_id,
           currency_code,
           SUM(debit)                     AS period_debit,
           SUM(credit)                    AS period_credit,
           SUM(debit_fc) - SUM(credit_fc) AS net_fc
    FROM gl_postings
    WHERE ledger = :ledger
      AND branch_id = :branch_id
      AND period_id = :period_id
    GROUP BY account_id, currency_code
)
INSERT INTO account_balances (
    period_id, ledger, branch_id, account_id, currency_code,
    opening_debit, opening_credit,
    period_debit, period_credit,
    closing_debit, closing_credit,
    opening_debit_fc, period_debit_fc,
    computed_at
)
SELECT :period_id,
       :ledger,
       :branch_id,
       account_id,
       currency_code,
       GREATEST(COALESCE(o.net, 0), 0),
       GREATEST(-COALESCE(o.net, 0), 0),
       COALESCE(m.period_debit, 0),
       COALESCE(m.period_credit, 0),
       GREATEST(COALESCE(o.net, 0) + COALESCE(m.period_debit, 0) - COALESCE(m.period_credit, 0), 0),
       GREATEST(-(COALESCE(o.net, 0) + COALESCE(m.period_debit, 0) - COALESCE(m.period_credit, 0)), 0),
       COALESCE(o.net_fc, 0),
       COALESCE(m.net_fc, 0),
       now()
FROM opening o
FULL OUTER JOIN movement m USING (account_id, currency_code)
-- Không chép mãi những khóa đã tất toán: dư đầu 0, nguyên tệ 0 và không có
-- phát sinh trong kỳ thì không có gì để trình bày — kỳ sau cũng không nhận
-- được gì từ nó. TK có phát sinh trong kỳ luôn giữ dòng, kể cả dư cuối về 0.
WHERE NOT (
    COALESCE(o.net, 0) = 0
    AND COALESCE(o.net_fc, 0) = 0
    AND m.account_id IS NULL
)
