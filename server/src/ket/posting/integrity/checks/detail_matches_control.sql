-- BR-GLE-05 ở phạm vi phase 4: sổ chi tiết theo đối tượng chỉ khớp được số
-- dư TK khi MỌI dòng phát sinh trên TK bật `detail_tracking` đều mang chiều
-- tương ứng — một dòng thiếu chiều là một khoản "không của ai" làm tổng chi
-- tiết hụt so với tổng hợp.
--
-- Validator `dimension_required` chặn từ lúc ghi sổ; check này bắt dữ liệu
-- đi vòng (SQL trực tiếp, gói cấu hình đổi `detail_tracking` SAU khi đã có
-- phát sinh). Nó đo CHIỀU CÓ MẶT, không đo SỐ TIỀN: một dòng đủ chiều vẫn có
-- thể mang số lệch với sổ chi tiết.
--
-- Phép đo số tiền là việc của một check khác, và nó chưa chạy được: bản thảo
-- `arap_matches_control.sql` đã có trong thư mục này nhưng **cố ý chưa vào
-- registry** — bốn nguồn báo-sai còn mở (bút toán tổng hợp gõ thẳng vào TK
-- công nợ, bên trái tính của TK lưỡng tính không có hóa đơn chi tiết, hóa đơn
-- rơi khi chuyển năm, và ranh giới năm giữa hai sổ phụ) được liệt kê ngay
-- trong đầu tệp ấy. Đối chiếu tồn kho thuộc phase 8.
--
-- `customer`/`vendor`/`employee` đòi đúng LOẠI đối tác (cùng luật
-- `PostingDimensions.has_tracking`): partner_kind 0 customer, 1 vendor,
-- 2 employee.
SELECT p.id            AS posting_id,
       v.id::text      AS voucher_id,
       v.voucher_no    AS voucher_no,
       p.ledger        AS ledger,
       p.line_no       AS line_no,
       a.code          AS account_code,
       t.tracking      AS tracking
FROM gl_postings p
JOIN chart_of_accounts a ON a.id = p.account_id
JOIN vouchers v ON v.id = p.voucher_id
CROSS JOIN LATERAL unnest(a.detail_tracking) AS t(tracking)
WHERE p.branch_id = :branch_id
  AND a.detail_tracking IS NOT NULL
  AND CASE t.tracking
        WHEN 'customer'     THEN p.partner_id IS NULL OR p.partner_kind IS DISTINCT FROM 0
        WHEN 'vendor'       THEN p.partner_id IS NULL OR p.partner_kind IS DISTINCT FROM 1
        WHEN 'employee'     THEN p.partner_id IS NULL OR p.partner_kind IS DISTINCT FROM 2
        WHEN 'cost_object'  THEN p.cost_object_id IS NULL
        WHEN 'project'      THEN p.project_id IS NULL
        WHEN 'order'        THEN p.order_id IS NULL
        WHEN 'contract'     THEN p.contract_id IS NULL
        WHEN 'expense_item' THEN p.expense_item_id IS NULL
        WHEN 'item'         THEN p.item_id IS NULL
        WHEN 'warehouse'    THEN p.warehouse_id IS NULL
        -- Giá trị tracking lạ = cấu hình hỏng: báo dòng ra chứ không nuốt.
        ELSE TRUE
      END
