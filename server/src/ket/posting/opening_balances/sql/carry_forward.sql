-- Chuyển số dư cuối năm nguồn thành số dư đầu năm nhận (FR-OPB-010) cho MỘT
-- (sổ, chi nhánh) — một câu INSERT … SELECT, không vòng lặp Python (LD-14).
--
-- Tham số:
--   :source_fiscal_year_id, :target_fiscal_year_id, :ledger, :branch_id
-- Chỗ trống bank_movements_rows (khuôn str.format — chỉ MỘT chỗ, trong thân
-- CTE bank_movements): job điền một dãy VALUES tham-số-hóa (hoặc SELECT-rỗng
-- khi không có gì) TRƯỚC khi chạy — KHÔNG phải bảng tạm, vì TEMPORARY đã bị
-- thu hồi khỏi role app/worker (roles.sql, chống che audit_log bằng pg_temp)
-- và mọi giá trị vẫn đi qua bound parameter.
--
-- Số cuối năm tính THẲNG từ nguồn sự thật: số dư ban đầu năm nguồn cộng mọi
-- phát sinh `gl_postings` của các kỳ năm nguồn — không đọc snapshot
-- `account_balances`, nên kết quả không phụ thuộc hàng đợi tính lại sạch hay
-- bẩn. Gộp theo TRỌN bộ chiều để "giữ nguyên chi tiết" (FR-OPB-010): đối
-- tượng công nợ, và cả các chiều 5 cột cố định mà chứng từ đã ghi.
--
-- Chiều TK NGÂN HÀNG (kind 1, lát 6D): job hỏi `DepositMovementSource` (kernel
-- Protocol, module bank cài) rồi đổ vào `carry_bank_movements`. Câu này cộng
-- mỗi con số ấy vào xô (TK, TK ngân hàng, tiền tệ) và trừ đúng chừng ấy khỏi
-- xô không-gắn-TK-ngân-hàng — tổng chuyển năm không đổi, chỉ được chia đúng
-- chỗ.
--
-- Từ lát 6G-1 nguồn ấy phủ MỌI chứng từ chạm 112x, không chỉ chứng từ tiền
-- gửi: chủ sở hữu nằm ở cột `gl_postings.bank_account_id`. Chỉ dòng ghi sổ
-- TRƯỚC 6G-1 mà migration không suy nổi chủ mới ở lại xô không gắn (kind 0) —
-- bịa một tài khoản cho chúng sẽ làm BR-BNK-01 "khớp" trên một con số dối.
--
-- CẦN XÉT Ở BƯỚC 22: cột ấy làm chính Protocol này thừa — `posting` gộp thẳng
-- `gl_postings.bank_account_id` được, không cần vòng qua module bank. Bỏ hay
-- giữ là quyết định của lượt đóng băng kernel, không phải của lát này.
--
-- detail_kind của dòng sinh ra: có bank_account_id → 1; còn lại suy từ
-- partner_kind (khách 2, NCC 3, nhân viên 4, còn lại 0). CẢNH BÁO PHASE 8:
-- nhóm 5–9 (tồn kho, dở dang, CCDC, TSCĐ, trả trước) bị LOẠI ở nhánh
-- opening_balances (detail_kind 0–4) và phát sinh mang item_id/warehouse_id
-- sẽ rơi về nhóm 0 — phase 8 phải mở rộng câu này (lớp FIFO còn lại, thẻ tài
-- sản) trước khi bật kho/tài sản, nếu không tổng chuyển sang năm sau thiếu
-- đúng phần tồn kho.
--
-- Tỷ giá của dòng mới là tỷ giá BÌNH QUÂN suy ra (quy đổi ÷ nguyên tệ) vì các
-- dòng nguồn khác tỷ giá đã gộp làm một; hai cột số tiền mới là sự thật, tỷ
-- giá chỉ để trình bày. GREATEST(0.000001) giữ ràng buộc exchange_rate > 0
-- khi phép chia làm tròn về 0; LEAST(...) giữ trần NUMERIC(18,6) — một cột
-- trình bày không được quyền làm đổ cả lượt chuyển (review 4C, M4).
--
-- Dòng "vừa Nợ VND vừa Có nguyên tệ" (net và net_fc trái dấu — nguồn gộp
-- nhiều tỷ giá + điều chỉnh) là kết quả CHỦ ĐÍCH: hai cột VND và hai cột
-- nguyên tệ tách GREATEST độc lập vì mỗi hệ tiền là một sự thật ròng riêng;
-- ép nguyên tệ về cùng bên với VND sẽ cần một số âm mà CHECK cấm, còn recalc
-- đọc cả hai bằng SUM(debit)-SUM(credit) nên số vẫn đúng (review 4C, M3 —
-- chấp nhận có ghi chú).
WITH source_periods AS (
    SELECT id FROM accounting_periods
    WHERE fiscal_year_id = :source_fiscal_year_id
),
bank_movements(account_id, bank_account_id, currency_code, net_fc, net) AS (
    {bank_movements_rows}
),
combined AS (
    SELECT account_id, currency_code, bank_account_id,
           partner_id, partner_kind, cost_object_id, project_id, order_id,
           contract_id, expense_item_id, item_id, warehouse_id, lot_id,
           SUM(debit)    - SUM(credit)    AS net,
           SUM(debit_fc) - SUM(credit_fc) AS net_fc
    FROM (
        SELECT account_id, currency_code, bank_account_id, partner_id,
               partner_kind, cost_object_id, project_id, order_id, contract_id,
               expense_item_id, item_id, warehouse_id, lot_id,
               debit, credit, debit_fc, credit_fc
        FROM opening_balances
        WHERE fiscal_year_id = :source_fiscal_year_id
          AND ledger = :ledger
          AND branch_id = :branch_id
          AND detail_kind BETWEEN 0 AND 4
        UNION ALL
        SELECT account_id, currency_code, NULL, partner_id, partner_kind,
               cost_object_id, project_id, order_id, contract_id,
               expense_item_id, item_id, warehouse_id, NULL,
               debit, credit, debit_fc, credit_fc
        FROM gl_postings
        WHERE ledger = :ledger
          AND branch_id = :branch_id
          AND period_id IN (SELECT id FROM source_periods)
        UNION ALL
        -- Cộng phát sinh tiền gửi vào xô của TK ngân hàng… (net âm hợp lệ:
        -- đây là dòng trung gian, SUM ở trên mới là con số thật)
        SELECT account_id, currency_code, bank_account_id, NULL, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               net, 0, net_fc, 0
        FROM bank_movements
        UNION ALL
        -- …và trừ đúng chừng ấy khỏi xô không gắn TK ngân hàng.
        SELECT account_id, currency_code, NULL, NULL, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               0, net, 0, net_fc
        FROM bank_movements
    ) united
    GROUP BY account_id, currency_code, bank_account_id, partner_id,
             partner_kind, cost_object_id, project_id, order_id, contract_id,
             expense_item_id, item_id, warehouse_id, lot_id
)
INSERT INTO opening_balances (
    fiscal_year_id, ledger, branch_id, account_id, currency_code, exchange_rate,
    partner_id, partner_kind, bank_account_id, cost_object_id, project_id,
    order_id, contract_id, expense_item_id, item_id, warehouse_id, lot_id,
    debit_fc, credit_fc, debit, credit, detail_kind
)
SELECT :target_fiscal_year_id,
       :ledger,
       :branch_id,
       account_id,
       currency_code,
       CASE
           WHEN net_fc <> 0 AND net / net_fc > 0
               THEN LEAST(GREATEST(ROUND(net / net_fc, 6), 0.000001), 999999999999.999999)
           ELSE 1
       END,
       partner_id, partner_kind, bank_account_id, cost_object_id, project_id,
       order_id, contract_id, expense_item_id, item_id, warehouse_id, lot_id,
       GREATEST(net_fc, 0),
       GREATEST(-net_fc, 0),
       GREATEST(net, 0),
       GREATEST(-net, 0),
       CASE
           WHEN bank_account_id IS NOT NULL THEN 1
           ELSE CASE partner_kind WHEN 0 THEN 2 WHEN 1 THEN 3 WHEN 2 THEN 4 ELSE 0 END
       END
FROM combined
WHERE net <> 0 OR net_fc <> 0
