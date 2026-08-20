-- Số liệu MỘT NĂM TÀI CHÍNH cho statement builder: mỗi tài khoản một dòng
-- (dư đầu năm ròng, phát sinh Nợ/Có lũy kế từ đầu năm tới :range_end — hai bộ,
-- xem dưới).
--
-- Tính THẲNG từ `opening_balances` + `gl_postings` — không đọc snapshot
-- `account_balances`: BCTC là con số pháp lý, không được phụ thuộc trạng thái
-- hàng đợi tính lại (kỳ bẩn hay sạch cho cùng một kết quả). `gl_postings` chỉ
-- chứa chứng từ ĐÃ ghi sổ nên BR-RPT-01 thỏa sẵn. RLS lọc chi nhánh trước khi
-- câu lệnh này thấy dữ liệu; :branch_id chỉ thu hẹp thêm để xem BCTC một chi
-- nhánh.
--
-- Cùng phép toán với `posting/balances/sql/trial_balance_direct.sql` (net theo
-- (TK, tiền tệ) rồi mới gộp) — BR-RPT-02: bảng cân đối TK và BCTC phải cho
-- cùng con số trên cùng dữ liệu.
--
-- Ranh giới đi bằng NGÀY (posting_date) — cùng caveat H1 với
-- trial_balance_direct.sql: kỳ 13 điều chỉnh (phase 10a) trùng khoảng ngày kỳ
-- 12 sẽ phá tương đương ngày↔kỳ, khi đó file này PHẢI đổi sang lọc theo
-- period_id.
--
-- **Hai bộ phát sinh trong MỘT lượt quét (LD-17, sửa H3 review 4F):**
--
-- * `turnover_*` — **mọi** bút toán. Số dư luôn suy từ bộ này
--   (`opening_net + turnover_debit - turnover_credit`), nên số dư **luôn đúng**
--   bất kể layout thuộc loại nào: bút toán kết chuyển là thứ làm 421 đúng, bỏ
--   nó khỏi số dư thì bảng cân đối không cân.
-- * `operating_*` — chỉ `entry_kind = 0` (phát sinh nghiệp vụ). B02 đo bộ này.
--
-- Bản đầu của lát 4F dùng một tham số boolean lọc thẳng CTE `movement`, nên
-- layout phát sinh nào lỡ dùng hàm số dư (`DR`/`CR`/`BAL`) sẽ nhận số dư
-- **thiếu kết chuyển** mà không có phép chặn nào — gói `.zip` nhập ngoài khai
-- công thức tự do nên đó là bề mặt thật, và `cashflow` (B03 của 10a) còn cần
-- cả hai chiều cùng lúc. Tính cả hai bộ bằng `FILTER` xóa hẳn lớp lỗi đó và
-- không tốn thêm lượt quét nào.
--
-- Tham số: :ledger, :fiscal_year_id, :year_start, :range_end, :branch_id
WITH opening AS (
    SELECT account_id, currency_code, SUM(debit) - SUM(credit) AS net
    FROM opening_balances
    WHERE fiscal_year_id = :fiscal_year_id
      AND ledger = :ledger
      AND (CAST(:branch_id AS INTEGER) IS NULL OR branch_id = :branch_id)
    GROUP BY account_id, currency_code
),
movement AS (
    SELECT account_id, currency_code,
           SUM(debit)                                AS turnover_debit,
           SUM(credit)                               AS turnover_credit,
           COALESCE(SUM(debit)  FILTER (WHERE entry_kind = 0), 0) AS operating_debit,
           COALESCE(SUM(credit) FILTER (WHERE entry_kind = 0), 0) AS operating_credit
    FROM gl_postings
    WHERE ledger = :ledger
      AND (CAST(:branch_id AS INTEGER) IS NULL OR branch_id = :branch_id)
      AND posting_date >= :year_start
      AND posting_date <= :range_end
    GROUP BY account_id, currency_code
),
merged AS (
    SELECT account_id,
           COALESCE(o.net, 0)               AS opening_net,
           COALESCE(m.turnover_debit, 0)    AS turnover_debit,
           COALESCE(m.turnover_credit, 0)   AS turnover_credit,
           COALESCE(m.operating_debit, 0)   AS operating_debit,
           COALESCE(m.operating_credit, 0)  AS operating_credit
    FROM opening o
    FULL OUTER JOIN movement m USING (account_id, currency_code)
)
SELECT a.code                                    AS account_code,
       SUM(g.opening_net)                        AS opening_net,
       SUM(g.turnover_debit)                     AS turnover_debit,
       SUM(g.turnover_credit)                    AS turnover_credit,
       SUM(g.operating_debit)                    AS operating_debit,
       SUM(g.operating_credit)                   AS operating_credit
FROM merged g
JOIN chart_of_accounts a ON a.id = g.account_id
GROUP BY a.code
HAVING NOT (
    SUM(g.opening_net) = 0
    AND SUM(g.turnover_debit) = 0
    AND SUM(g.turnover_credit) = 0
)
