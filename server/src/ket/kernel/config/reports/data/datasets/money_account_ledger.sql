-- Dataset `money_account_ledger`: sổ chi tiết một tài khoản TIỀN kèm số dư lũy
-- kế sau từng dòng. Phủ HAI mẫu sổ bằng đúng một câu, khác nhau ở tham số ghim
-- `account_prefix` (`ReportDefinition.fixed_params`):
--
--   * `111` → **S07a-DN** Sổ kế toán chi tiết quỹ tiền mặt
--   * `112` → **S08-DN**  Sổ tiền gửi không kỳ hạn
--
-- Hai mẫu có cùng hình dạng (ngày ghi sổ, chứng từ, diễn giải, TK đối ứng,
-- phát sinh Nợ/Có, số tồn) và chỉ khác nhãn cột + chiều gộp, nên tách thành
-- hai dataset sẽ là nhân đôi cùng một câu SQL — chính thứ `fixed_params` sinh
-- ra để tránh.
--
-- **Chiều gộp thứ hai: TỪNG tài khoản ngân hàng.** S08-DN là sổ của MỘT tài
-- khoản tiền gửi ("Nơi mở tài khoản giao dịch", "Số hiệu tài khoản tại nơi
-- gửi"), nhưng `gl_postings` chỉ mang TK kế toán (1121) chứ không mang tài
-- khoản ngân hàng. Chủ sở hữu đọc thẳng cột `gl_postings.bank_account_id`
-- (chiều `bank_account`, lát 6G-1); luật quy chủ — BC/UNC/SEC theo thân chứng
-- từ, chuyển tiền nội bộ thì bên Nợ thuộc tài khoản ĐÍCH — sống ở đường GHI
-- (`bank/posting_mapper._deposit_owner`) và không còn được chép lại ở đây.
--
-- Trước 6G-1 chủ sở hữu suy bằng `LEFT JOIN bank_vouchers` ngay trong câu này,
-- nên bút toán GLE gõ thẳng 112x và **chứng từ QUỸ chạm 112** (gói builtin khai
-- sẵn `PT rut-tgnh-nhap-quy` và chiều ngược bằng phiếu chi) rơi vào nhóm
-- `(chưa gắn)` — nghiệp vụ HẰNG TUẦN làm S08-DN lệch sao kê đúng bằng chúng
-- (review 6E-1 H-3). Nhóm `(chưa gắn)` GIỮ LẠI cho dữ liệu ghi sổ trước lát
-- này mà migration không suy nổi chủ: hiện ra thay vì bị giấu, không bịa chủ.
--
-- Số dư đầu kỳ = dư đầu năm (`opening_balances` của năm chứa :from_date, đã
-- mang sẵn `bank_account_id` từ 0019) + phát sinh từ đầu năm tới trước
-- :from_date — cùng phép toán với `gl_detail.sql`/`trial_balance_direct.sql`
-- (BR-RPT-02: hai báo cáo phải cho cùng con số).
--
-- Lọc chi nhánh + sổ **bên trong** (`supports_branch/ledger = false`): số dư
-- lũy kế phải tính trên đúng tập dòng hiển thị — lọc ở lớp bọc ngoài làm số
-- tồn nhảy. RLS vẫn là cô lập thật (RT-04).
--
-- Ranh giới đi bằng NGÀY — cùng caveat kỳ-13 với `gl_detail.sql`: khi 10a sinh
-- kỳ điều chỉnh trùng khoảng ngày, tệp này phải đổi sang lọc theo period_id.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids, :account_prefix,
-- :account_code (một TK kế toán cụ thể; bỏ trống = mọi TK thuộc nhóm)
WITH fy AS (
    SELECT id, start_date
    FROM fiscal_years
    WHERE :from_date >= start_date AND :from_date <= end_date
    -- Cùng phép chọn xác định với `periods.service.fiscal_year_covering`
    -- (review 6E-1 M-6): `fiscal_years` KHÔNG có ràng buộc DB chống chồng lấn,
    -- và `JOIN fy ON TRUE` với hai năm cùng phủ một ngày sẽ nhân đôi nhánh
    -- phát sinh của số dư đầu kỳ — cho kết quả khác thẻ số dư BFF trên cùng
    -- dữ liệu. Hai cài đặt của một khái niệm phải chọn giống nhau.
    ORDER BY start_date DESC
    LIMIT 1
),
-- CTE dòng phát sinh trên TK tiền, một lần, dùng lại cho cả nhánh số dư đầu kỳ
-- lẫn nhánh dòng trong kỳ.
postings AS (
    SELECT p.account_id,
           p.posting_date,
           p.voucher_id,
           p.line_no,
           p.id,
           p.debit,
           p.credit,
           p.description,
           p.corresponding_account_id,
           p.bank_account_id
    FROM gl_postings p
    JOIN chart_of_accounts coa ON coa.id = p.account_id
    WHERE p.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
      AND coa.code LIKE :account_prefix || '%'
      AND (CAST(:account_code AS TEXT) IS NULL OR coa.code = :account_code)
      -- Cận trên NGÀY cho CTE dùng chung (review 6E-1 M-5): thiếu nó thì in sổ
      -- MỘT tháng vẫn quét + vật chất hóa mọi dòng 111/112 từ ngày mở sổ, vì
      -- CTE được tham chiếu hai lần nên PostgreSQL materialize.
      AND p.posting_date <= :to_date
),
opening_source AS (
    SELECT ob.account_id,
           ob.bank_account_id,
           SUM(ob.debit) - SUM(ob.credit) AS net
    FROM opening_balances ob
    JOIN chart_of_accounts coa ON coa.id = ob.account_id
    JOIN fy ON ob.fiscal_year_id = fy.id
    WHERE ob.ledger = :ledger
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR ob.branch_id = ANY(:branch_ids))
      AND coa.code LIKE :account_prefix || '%'
      AND (CAST(:account_code AS TEXT) IS NULL OR coa.code = :account_code)
    GROUP BY ob.account_id, ob.bank_account_id
    UNION ALL
    SELECT p.account_id, p.bank_account_id, SUM(p.debit) - SUM(p.credit)
    FROM postings p
    JOIN fy ON TRUE
    WHERE p.posting_date >= fy.start_date
      AND p.posting_date < :from_date
    GROUP BY p.account_id, p.bank_account_id
),
opening AS (
    SELECT coa.code            AS account_code,
           MIN(coa.name)       AS account_name,
           o.bank_account_id,
           SUM(o.net)          AS net
    FROM opening_source o
    JOIN chart_of_accounts coa ON coa.id = o.account_id
    GROUP BY coa.code, o.bank_account_id
),
lines AS (
    SELECT coa.code                               AS account_code,
           coa.name                               AS account_name,
           p.bank_account_id,
           p.posting_date,
           v.voucher_no,
           v.document_date,
           COALESCE(p.description, v.description) AS description,
           corr.code                              AS corresponding_account,
           p.debit,
           p.credit,
           SUM(p.debit - p.credit) OVER w         AS running_net,
           ROW_NUMBER() OVER w                    AS rn
    FROM postings p
    JOIN vouchers v ON v.id = p.voucher_id
    JOIN chart_of_accounts coa ON coa.id = p.account_id
    LEFT JOIN chart_of_accounts corr ON corr.id = p.corresponding_account_id
    WHERE p.posting_date >= :from_date
      AND p.posting_date <= :to_date
    -- Định danh chứng từ trong khóa sắp (bài học H1 lát 5D): đánh số theo chi
    -- nhánh làm hai chứng từ trùng số cùng ngày thành chuyện thường ngày, và
    -- dòng của chúng không được đan vào nhau.
    WINDOW w AS (
        PARTITION BY coa.code, p.bank_account_id
        ORDER BY p.posting_date, v.voucher_no, v.branch_id, p.voucher_id, p.line_no, p.id
    )
),
-- Cặp (TK kế toán, TK ngân hàng) nào có mặt trên sổ: có dư đầu kỳ khác 0 HOẶC
-- có phát sinh trong khoảng — cả hai đều mở đầu bằng dòng "Số tồn đầu kỳ".
account_universe AS (
    SELECT u.account_code, u.bank_account_id, MIN(u.account_name) AS account_name
    FROM (
        SELECT account_code, bank_account_id, account_name FROM opening WHERE net <> 0
        UNION ALL
        SELECT DISTINCT account_code, bank_account_id, account_name FROM lines
    ) u
    GROUP BY u.account_code, u.bank_account_id
),
rows_out AS (
    SELECT au.account_code,
           au.account_name,
           au.bank_account_id,
           0                             AS sort_seq,
           CAST(0 AS BIGINT)             AS rn,
           CAST(NULL AS DATE)            AS posting_date,
           CAST(NULL AS DATE)            AS document_date,
           CAST(NULL AS TEXT)            AS voucher_no,
           'Số tồn đầu kỳ'               AS description,
           CAST(NULL AS TEXT)            AS corresponding_account,
           CAST(NULL AS NUMERIC)         AS debit,
           CAST(NULL AS NUMERIC)         AS credit,
           COALESCE(o.net, 0)            AS balance
    FROM account_universe au
    LEFT JOIN opening o
           ON o.account_code = au.account_code
          AND o.bank_account_id IS NOT DISTINCT FROM au.bank_account_id
    UNION ALL
    SELECT l.account_code,
           l.account_name,
           l.bank_account_id,
           1                             AS sort_seq,
           l.rn,
           l.posting_date,
           l.document_date,
           l.voucher_no,
           l.description,
           l.corresponding_account,
           l.debit,
           l.credit,
           COALESCE(o.net, 0) + l.running_net AS balance
    FROM lines l
    LEFT JOIN opening o
           ON o.account_code = l.account_code
          AND o.bank_account_id IS NOT DISTINCT FROM l.bank_account_id
)
SELECT r.account_code,
       r.account_name,
       COALESCE(cba.code, '(chưa gắn)')                      AS money_account_code,
       COALESCE(
           cba.name || COALESCE(' — ' || bank.name, ''),
           '(chưa gắn tài khoản ngân hàng)'
       )                                                     AS money_account_name,
       r.sort_seq,
       r.rn,
       r.posting_date,
       r.document_date,
       r.voucher_no,
       r.description,
       r.corresponding_account,
       r.debit,
       r.credit,
       r.balance
FROM rows_out r
LEFT JOIN company_bank_accounts cba ON cba.id = r.bank_account_id
LEFT JOIN banks bank ON bank.id = cba.bank_id
