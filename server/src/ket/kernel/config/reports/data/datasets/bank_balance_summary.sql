-- Dataset `bank_balance_summary`: Bảng kê số dư ngân hàng (`docs/srs/04` §5 #2,
-- FR-BNK-041) — số dư đầu kỳ, phát sinh thu/chi, số dư cuối kỳ theo TỪNG tài
-- khoản ngân hàng và TỪNG loại tiền, hiện đồng thời nguyên tệ và quy đổi.
--
-- Chủ sở hữu của một dòng 112x đọc thẳng cột `gl_postings.bank_account_id`
-- (chiều `bank_account`, lát 6G-1). Luật quy chủ — BC/UNC/SEC theo thân chứng
-- từ, chuyển tiền nội bộ thì bên Nợ thuộc tài khoản ĐÍCH — sống ở đường GHI,
-- trong `bank/posting_mapper._deposit_owner`, và báo cáo không còn chép lại nó.
--
-- Trước 6G-1 chủ sở hữu được suy bằng `LEFT JOIN bank_vouchers` ngay trong câu
-- này, nên hai nguồn phát sinh rơi vào nhóm `(chưa gắn)`: bút toán GLE gõ
-- thẳng 112x, và **chứng từ QUỸ chạm 112** (gói builtin khai sẵn
-- `PT rut-tgnh-nhap-quy` và chiều ngược bằng phiếu chi) — nghiệp vụ HẰNG TUẦN
-- làm số dư S08-DN lệch sao kê đúng bằng chúng (review 6E-1 H-3).
--
-- Nhóm `(chưa gắn)` GIỮ LẠI cho dữ liệu ghi sổ trước lát 6G-1 mà migration
-- không suy nổi chủ: hiện ra thay vì bị giấu, không bịa chủ.
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
    -- Cùng phép chọn xác định với `periods.service.fiscal_year_covering`
    -- (review 6E-1 M-6): `fiscal_years` KHÔNG có ràng buộc DB chống chồng lấn,
    -- và `JOIN fy ON TRUE` với hai năm cùng phủ một ngày sẽ nhân đôi nhánh
    -- phát sinh của số dư đầu kỳ — cho kết quả khác thẻ số dư BFF trên cùng
    -- dữ liệu. Hai cài đặt của một khái niệm phải chọn giống nhau.
    ORDER BY start_date DESC
    LIMIT 1
),
postings AS (
    SELECT p.posting_date,
           p.currency_code,
           p.debit,
           p.credit,
           p.debit_fc,
           p.credit_fc,
           p.bank_account_id
    FROM gl_postings p
    JOIN chart_of_accounts coa ON coa.id = p.account_id
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
