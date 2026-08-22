-- Dataset `bank_reconciliation`: Đối chiếu ngân hàng (`docs/srs/04` §4.3) —
-- món nợ lát 6D bàn giao cho 6E ("báo cáo metadata đối chiếu in được").
--
-- Lát 6D đã có đường đối chiếu sống (nhập sao kê → khớp tự động → khớp tay →
-- gỡ) và API trả chênh lệch hai phía cho màn hình. Cái còn thiếu là bản
-- **in/xuất được** đi qua chính hạ tầng báo cáo metadata — đó là tệp này.
--
-- Ba nhóm dòng, hợp lại thành một bảng đối chiếu đọc từ trên xuống:
--   1. `da-khop`      — dòng sao kê đã ghép với chứng từ (hai vế, lệch = 0)
--   2. `sao-ke-chua`  — dòng sao kê chưa có chứng từ đối ứng
--   3. `chung-tu-chua`— chứng từ tiền gửi đã ghi sổ chưa xuất hiện trên sao kê
--
-- Đơn vị so khớp giữ ĐÚNG định nghĩa của lát 6D, không phát minh lại: phát
-- sinh RÒNG nguyên tệ trên TK 112x của chứng từ ĐÃ ghi sổ, đọc `gl_postings`
-- **sổ tài chính** (ledger 0 — thiếu bộ lọc này thì hai hệ thống sổ nhân đôi
-- mọi con số, đúng cái bẫy test 6D đã bắt), và chuyển tiền nội bộ quy chủ
-- theo CHIỀU dòng (`bank_vouchers.kind = 3`: dòng Nợ thuộc tài khoản đích).
-- Một chứng từ CTNB hợp lệ đứng trên sao kê của CẢ HAI tài khoản, nên mọi
-- phép gộp ở đây đi theo cặp (chứng từ, tài khoản ngân hàng) chứ không theo
-- chứng từ — trùng khuôn unique "một chứng từ một dòng THEO TÀI KHOẢN" mà 6D
-- đã phải sửa sau khi test bắt được thiết kế unique toàn cục.
--
-- `match_kind` trên `bank_statement_lines`: 0 = chưa khớp. Dòng đã khớp mang
-- `matched_voucher_id` khác NULL — dùng chính cột đó làm mốc, không đoán theo
-- `match_kind`.
--
-- **Dấu của dòng sao kê là `credit - debit`** (Ghi Có trên sao kê = tiền VÀO
-- tài khoản), cùng chiều với `net_fc` của chứng từ. Đây là định nghĩa của
-- `reconciliation._line_amount`, nơi đường khớp thật đọc — báo cáo phải dùng
-- CÙNG một quy ước, nếu không mọi dòng đã khớp sẽ hiện chênh lệch bằng hai
-- lần số tiền.
--
-- Lọc chi nhánh **bên trong** (`supports_branch = false`): sao kê thuộc mức
-- TÀI KHOẢN NGÂN HÀNG chứ không mức chi nhánh, còn chứng từ nằm dưới RLS chi
-- nhánh — thu hẹp một vế mà không thu hẹp vế kia sẽ đẻ ra chênh lệch giả.
-- Cùng lập luận M-1 của lát 6D (auto-match đòi phạm vi MỌI chi nhánh).
-- `supports_ledger = false` vì sổ đã cố định là sổ tài chính như trên.
--
-- Tham số: :from_date, :to_date, :bank_account_id
WITH accounts AS (
    SELECT cba.id,
           cba.code AS bank_account_code,
           cba.name AS bank_account_name,
           b.name   AS bank_name
    FROM company_bank_accounts cba
    LEFT JOIN banks b ON b.id = cba.bank_id
    WHERE CAST(:bank_account_id AS INTEGER) IS NULL OR cba.id = :bank_account_id
),
-- Chứng từ ĐÃ được ghép với một dòng sao kê BẤT KỲ — không giới hạn ngày, cùng
-- lý do với `voucher_movements` bên dưới (H-2). "Chứng từ chưa có trên sao kê"
-- phải nghĩa là chưa ai ghép nó, không phải "dòng sao kê ghép với nó rơi ra
-- ngoài cửa sổ đang xem".
matched_voucher_ids AS (
    SELECT DISTINCT l.matched_voucher_id AS voucher_id, l.bank_account_id
    FROM bank_statement_lines l
    JOIN accounts a ON a.id = l.bank_account_id
    WHERE l.matched_voucher_id IS NOT NULL
),
statement_lines AS (
    SELECT l.id,
           l.bank_account_id,
           l.txn_date,
           l.reference_no,
           l.description,
           l.debit,
           l.credit,
           l.matched_voucher_id
    FROM bank_statement_lines l
    JOIN accounts a ON a.id = l.bank_account_id
    WHERE l.txn_date >= :from_date
      AND l.txn_date <= :to_date
),
-- Phát sinh ròng nguyên tệ trên TK tiền gửi của từng chứng từ ngân hàng ĐÃ
-- ghi sổ, quy chủ theo chiều dòng cho chuyển tiền nội bộ.
--
-- **Không lọc ngày ở đây.** Khớp là quan hệ 1-1 trên `matched_voucher_id`, chứ
-- không phải một phép so trong cửa sổ: auto-match của 6D cố ý chấp nhận lệch
-- ±3 ngày, nên một cặp đã khớp bắc qua ngày 30/31 là chuyện thường tháng. Cắt
-- ngày ở CTE này sẽ làm chính cặp đó hiện thành HAI dòng lệch giả — một dòng
-- "Đã khớp" không tìm thấy chứng từ ở kỳ sau, một dòng "chứng từ chưa có trên
-- sao kê" ở kỳ trước — mà tổng cả năm vẫn đúng, nên không ai tra ra (review
-- 6E-1 H-2). Cửa sổ ngày chỉ áp cho nhóm 3 (chứng từ CHƯA khớp), bên dưới.
voucher_movements AS (
    SELECT p.voucher_id,
           CASE
               WHEN bv.kind = 3 AND p.debit > 0 THEN bv.counter_bank_account_id
               ELSE bv.bank_account_id
           END                                   AS bank_account_id,
           v.voucher_no,
           v.posting_date,
           v.description,
           bv.reference_no,
           SUM(p.debit_fc - p.credit_fc)         AS net_fc
    FROM gl_postings p
    JOIN bank_vouchers bv ON bv.id = p.voucher_id
    JOIN vouchers v ON v.id = p.voucher_id
    JOIN chart_of_accounts coa ON coa.id = p.account_id
    WHERE p.ledger = 0
      AND coa.code LIKE '112%'
      AND v.status = 2
    GROUP BY p.voucher_id, 2, v.voucher_no, v.posting_date, v.description, bv.reference_no
),
movements AS (
    SELECT m.*
    FROM voucher_movements m
    JOIN accounts a ON a.id = m.bank_account_id
    WHERE m.net_fc <> 0
)
-- 1. Dòng sao kê đã ghép với chứng từ.
--
-- **Chứng từ nằm NGOÀI phạm vi người đọc là ca có thật, không phải lỗi dữ
-- liệu** (review 6E-1 H-1): `bank_statement_lines` không mang chiều chi nhánh
-- (danh mục + sao kê ở mức TÀI KHOẢN ngân hàng), còn vế chứng từ thì luôn bị
-- RLS thu hẹp. Người chỉ thuộc chi nhánh B mở báo cáo sẽ thấy dòng sao kê của
-- một chứng từ chi nhánh A mà họ không được đọc.
--
-- Khi ấy KHÔNG được in `difference` bằng số: `m.net_fc` vắng mặt vì phạm vi
-- chứ không vì thiếu chứng từ, nên `credit - debit - 0` sẽ là một con số lệch
-- BỊA — và hai người đọc cùng báo cáo, cùng kỳ, ra hai con số khác nhau. Một
-- bảng đối chiếu mà số phụ thuộc người mở thì không dùng làm bằng chứng được.
-- Dòng đó mang nhãn riêng và `difference` NULL: nói đúng thứ mình biết.
SELECT a.bank_account_code,
       a.bank_account_name,
       a.bank_name,
       0                                   AS group_seq,
       CASE WHEN m.voucher_id IS NULL
            THEN 'Đã khớp với chứng từ ngoài phạm vi'
            ELSE 'Đã khớp'
       END                                 AS status,
       s.txn_date                          AS statement_date,
       s.reference_no                      AS statement_reference,
       s.description                       AS statement_description,
       s.credit                            AS statement_receipt,
       s.debit                             AS statement_payment,
       m.voucher_no,
       m.posting_date,
       m.net_fc                            AS voucher_net,
       CASE WHEN m.voucher_id IS NULL
            THEN CAST(NULL AS NUMERIC)
            ELSE s.credit - s.debit - m.net_fc
       END                                 AS difference
FROM statement_lines s
JOIN accounts a ON a.id = s.bank_account_id
LEFT JOIN movements m
       ON m.voucher_id = s.matched_voucher_id
      AND m.bank_account_id = s.bank_account_id
WHERE s.matched_voucher_id IS NOT NULL
UNION ALL
-- 2. Dòng sao kê chưa có chứng từ.
SELECT a.bank_account_code,
       a.bank_account_name,
       a.bank_name,
       1                                   AS group_seq,
       'Sao kê chưa có chứng từ'           AS status,
       s.txn_date,
       s.reference_no,
       s.description,
       s.credit,
       s.debit,
       CAST(NULL AS TEXT)                  AS voucher_no,
       CAST(NULL AS DATE)                  AS posting_date,
       CAST(NULL AS NUMERIC)               AS voucher_net,
       s.credit - s.debit                  AS difference
FROM statement_lines s
JOIN accounts a ON a.id = s.bank_account_id
WHERE s.matched_voucher_id IS NULL
UNION ALL
-- 3. Chứng từ đã ghi sổ chưa xuất hiện trên sao kê.
SELECT a.bank_account_code,
       a.bank_account_name,
       a.bank_name,
       2                                   AS group_seq,
       'Chứng từ chưa có trên sao kê'      AS status,
       CAST(NULL AS DATE)                  AS statement_date,
       m.reference_no,
       m.description,
       CAST(NULL AS NUMERIC)               AS statement_receipt,
       CAST(NULL AS NUMERIC)               AS statement_payment,
       m.voucher_no,
       m.posting_date,
       m.net_fc,
       -m.net_fc                           AS difference
FROM movements m
JOIN accounts a ON a.id = m.bank_account_id
-- Cửa sổ ngày áp ở ĐÂY (chứng từ chưa khớp), không ở CTE — xem H-2 bên trên.
WHERE m.posting_date >= :from_date
  AND m.posting_date <= :to_date
  AND NOT EXISTS (
        SELECT 1
        FROM matched_voucher_ids mv
        WHERE mv.voucher_id = m.voucher_id
          AND mv.bank_account_id = m.bank_account_id
      )
