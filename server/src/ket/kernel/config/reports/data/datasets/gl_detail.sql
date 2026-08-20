-- Dataset `gl_detail`: Sổ chi tiết các tài khoản (S38-DN / S19-DNN) — từng
-- dòng phát sinh của một/mọi tài khoản kèm **số dư lũy kế sau từng dòng**
-- (cột "Số dư Nợ/Có" của mẫu) và một dòng tổng hợp "Số dư đầu kỳ" mở đầu
-- mỗi tài khoản.
--
-- Số dư đầu kỳ = dư đầu năm (`opening_balances` của năm tài chính chứa
-- :from_date) + phát sinh từ đầu năm tới trước :from_date — cùng phép toán
-- với `posting/balances/sql/trial_balance_direct.sql` (BR-RPT-02: hai báo cáo
-- phải cho cùng con số). Gộp theo SỐ HIỆU TK, không theo `account_id` (trục
-- gộp LD-17/4F: đổi gói cấu hình không tách đôi một tài khoản).
--
-- Lọc chi nhánh + sổ **bên trong** (`supports_branch/ledger = false`): số dư
-- lũy kế tính trên đúng tập dòng được hiển thị, lọc ở lớp bọc ngoài sẽ cho số
-- dư "nhảy" vì cộng cả dòng đã bị ẩn. RLS vẫn là cô lập thật (RT-04).
--
-- Dòng "Số dư đầu kỳ" là arm UNION riêng: `sort_seq = 0` xếp nó lên đầu mỗi
-- tài khoản; layout sort theo (account_code, sort_seq, rn) nên thứ tự hiển
-- thị TRÙNG với thứ tự cửa sổ tính lũy kế — điều kiện để "số dư sau dòng
-- này" đúng theo nghĩa đen.
--
-- Ranh giới đi bằng NGÀY — cùng caveat kỳ-13 với trial_balance_direct.sql:
-- khi 10a sinh kỳ điều chỉnh trùng khoảng ngày, file này phải đổi sang lọc
-- theo period_id.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids, :account_code
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
opening AS (
    SELECT coa.code AS account_code, MIN(coa.name) AS account_name, SUM(o.net) AS net
    FROM opening_source o
    JOIN chart_of_accounts coa ON coa.id = o.account_id
    WHERE CAST(:account_code AS TEXT) IS NULL OR coa.code = :account_code
    GROUP BY coa.code
),
lines AS (
    SELECT
        coa.code                               AS account_code,
        coa.name                               AS account_name,
        p.posting_date,
        v.voucher_no,
        v.document_date,
        COALESCE(p.description, v.description) AS description,
        corr.code                              AS corresponding_account,
        p.debit,
        p.credit,
        SUM(p.debit - p.credit) OVER w         AS running_net,
        ROW_NUMBER() OVER w                    AS rn
    FROM gl_postings p
    JOIN vouchers v ON v.id = p.voucher_id
    JOIN chart_of_accounts coa ON coa.id = p.account_id
    LEFT JOIN chart_of_accounts corr ON corr.id = p.corresponding_account_id
    WHERE p.ledger = :ledger
      AND p.posting_date >= :from_date
      AND p.posting_date <= :to_date
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
      AND (CAST(:account_code AS TEXT) IS NULL OR coa.code = :account_code)
    -- Định danh chứng từ trong khóa sắp — cùng lý do H1 của gl_journal.sql:
    -- hai chứng từ trùng số không được đan dòng vào nhau.
    WINDOW w AS (
        PARTITION BY coa.code
        ORDER BY p.posting_date, v.voucher_no, v.branch_id, p.voucher_id, p.line_no, p.id
    )
),
-- Tài khoản nào có mặt trên sổ: có dư đầu kỳ khác 0 HOẶC có phát sinh trong
-- khoảng — cả hai đều phải mở đầu bằng dòng "Số dư đầu kỳ" (bằng 0 khi tài
-- khoản mới phát sinh lần đầu trong kỳ).
account_universe AS (
    SELECT u.account_code, MIN(u.account_name) AS account_name
    FROM (
        SELECT account_code, account_name FROM opening WHERE net <> 0
        UNION ALL
        SELECT DISTINCT account_code, account_name FROM lines
    ) u
    GROUP BY u.account_code
)
SELECT
    au.account_code,
    au.account_name,
    0                            AS sort_seq,
    CAST(0 AS BIGINT)            AS rn,
    CAST(NULL AS DATE)           AS posting_date,
    CAST(NULL AS TEXT)           AS voucher_no,
    CAST(NULL AS DATE)           AS document_date,
    'Số dư đầu kỳ'               AS description,
    CAST(NULL AS TEXT)           AS corresponding_account,
    CAST(NULL AS NUMERIC)        AS debit,
    CAST(NULL AS NUMERIC)        AS credit,
    GREATEST(COALESCE(o.net, 0), 0)  AS balance_debit,
    GREATEST(-COALESCE(o.net, 0), 0) AS balance_credit
FROM account_universe au
LEFT JOIN opening o ON o.account_code = au.account_code
UNION ALL
SELECT
    l.account_code,
    l.account_name,
    1                            AS sort_seq,
    l.rn,
    l.posting_date,
    l.voucher_no,
    l.document_date,
    l.description,
    l.corresponding_account,
    l.debit,
    l.credit,
    GREATEST(COALESCE(o.net, 0) + l.running_net, 0)  AS balance_debit,
    GREATEST(-(COALESCE(o.net, 0) + l.running_net), 0) AS balance_credit
FROM lines l
LEFT JOIN opening o ON o.account_code = l.account_code
