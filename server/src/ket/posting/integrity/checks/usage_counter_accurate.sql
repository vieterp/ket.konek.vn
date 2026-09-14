-- `master_data_usage` khớp tham chiếu thực tế (FR-NFR-007, BR-SYS-02).
--
-- Bộ đếm là dữ liệu DẪN XUẤT: nó chỉ trung thực khi mọi đường ghi chứng từ
-- nhớ gọi `record_use` — và chỗ bắt lỗi "quên gọi" chính là check này
-- (docstring `kernel/master_data/usage.py` chỉ thẳng sang đây).
--
-- Nguồn đếm khai theo module, UNION theo `entity_type` — mỗi module ghi
-- chứng từ bổ sung nhánh của mình:
--
-- * `cash_book` (lát 6B): đối tác trên phiếu thu/chi (header + từng dòng).
--   `partner_kind` 0/1 → `partners`, 2 → `employees` — cùng ánh xạ
--   `_USAGE_TABLE_BY_PARTNER_KIND` của `modules/cash_book/service.py`.
-- * `bank` (lát 6C): đối tác trên chứng từ tiền gửi (header + dòng) cùng ánh
--   xạ trên, VÀ tài khoản ngân hàng doanh nghiệp (`company_bank_accounts`) —
--   TK nguồn của mọi chứng từ + TK đích của chuyển nội bộ (nợ 6A).
-- * `cash_book` (lát 6G-1): thêm chiều `bank_account` trên dòng phiếu quỹ —
--   cùng danh mục `company_bank_accounts`.
-- * sao kê ngân hàng (`bank_statements`, nhánh bổ sung ở 6G-1): mỗi sao kê đã
--   nhập giữ một tham chiếu tới TK ngân hàng của nó.
-- * `purchase` (lát 7B): nhà cung cấp trên hóa đơn mua (header) và nhà cung
--   cấp dịch vụ trên từng dòng chi phí mua hàng (`landed_costs.vendor_id`) —
--   luôn là `partners`, cùng `_usage_of` của `modules/purchase/service.py`.
-- * `sales` (lát 7C-2): khách hàng trên hóa đơn bán, và nhân viên bán hàng khi
--   được khai (`partner_kind = 2` → `employees`).
-- * `purchase` (lát 7G-1, nhánh bổ sung ở 7G-2a): người mua trên hóa đơn mua
--   (`purchase_invoices.buyer_id` → `employees`).
-- * `sales` (lát 7G-2a): mã quy cách trên từng DÒNG hóa đơn bán
--   (`sales_invoice_lines.variant_id` → `item_variants`).
--
-- **Hai lát liên tiếp thêm một đường `record_use` mà quên nhánh đối chiếu của
-- nó** (7G-1 với `buyer_id`, 7G-2a với `variant_id`), và cả hai lần hậu quả
-- giống nhau: check ĐỎ trên dữ liệu ĐÚNG, tức tiếng chuông duy nhất báo "ai đó
-- quên `record_use`" chìm trong tiếng ồn đã biết. Thêm một đường đếm thì phải
-- thêm nhánh ở đây trong cùng lượt sửa; `test_the_usage_check_is_clean_on_
-- correct_books` là bài kiểm canh điều đó.
--
-- Bút toán tổng hợp (`gl_journal_lines`) cố ý ĐỨNG NGOÀI bộ đếm, như từ đầu:
-- nó không gọi `record_use` cho chiều nào cả. Vì thế lượt chuyển số dư đầu năm
-- KHÔNG được dựa vào bộ đếm để bảo đảm TK ngân hàng còn sống — nó tự hạ dòng
-- mồ côi về nhóm chưa-gắn (xem `sql/carry_forward.sql`).
--
-- FULL JOIN hai phía: bộ đếm có mà không ai tham chiếu → lệch; tham chiếu có
-- mà bộ đếm thiếu/khác → lệch. Đường ghi nào quên `record_use` lộ ra ở đây.
WITH partner_refs AS (
    SELECT CASE WHEN partner_kind = 2 THEN 'employees' ELSE 'partners' END AS entity_type,
           partner_id AS entity_id
    FROM (
        SELECT partner_kind, partner_id
        FROM cash_vouchers
        WHERE partner_id IS NOT NULL
        UNION ALL
        SELECT partner_kind, partner_id
        FROM cash_voucher_lines
        WHERE partner_id IS NOT NULL
        UNION ALL
        SELECT partner_kind, partner_id
        FROM bank_vouchers
        WHERE partner_id IS NOT NULL
        UNION ALL
        SELECT partner_kind, partner_id
        FROM bank_voucher_lines
        WHERE partner_id IS NOT NULL
        UNION ALL
        SELECT 1, vendor_id
        FROM purchase_invoices
        UNION ALL
        SELECT 1, vendor_id
        FROM landed_costs
        WHERE vendor_id IS NOT NULL
        UNION ALL
        -- Hóa đơn bán (lát 7C-2) đếm hai chiều: khách hàng luôn có, nhân viên
        -- bán hàng khi được khai. `partner_kind = 2` là lối vào 'employees' ở
        -- CASE bên trên — nhân viên bán hàng là tham chiếu đầu tiên tới bảng
        -- ấy từ một chứng từ, nên trước lát này nhánh 'employees' của phép
        -- kiểm chưa có nguồn nào.
        SELECT 0, customer_id
        FROM sales_invoices
        UNION ALL
        SELECT 2, salesperson_id
        FROM sales_invoices
        WHERE salesperson_id IS NOT NULL
        UNION ALL
        -- Người mua trên hóa đơn mua (lát 7G-1) — nhánh này THIẾU từ chính lát
        -- ấy: `PurchaseInvoiceService._usage_of` đếm `(employees, buyer_id)`
        -- nhưng phía tham chiếu không có nguồn nào, nên check ĐỎ ngay hóa đơn
        -- mua đầu tiên khai người mua (bộ đếm nói 1, phía tham chiếu nói 0).
        -- Cùng hình dạng bỏ sót với `bank_statements` ở khối dưới, và là lần
        -- thứ hai trong hai lát liên tiếp: thêm một đường `record_use` mà không
        -- thêm nhánh đối chiếu của nó.
        SELECT 2, buyer_id
        FROM purchase_invoices
        WHERE buyer_id IS NOT NULL
    ) refs
),
variant_refs AS (
    -- Mã quy cách trên dòng hóa đơn bán (lát 7G-2a). `sales_invoice_lines.
    -- variant_id` cố ý KHÔNG có khóa ngoại (ràng buộc thật sự là một CẶP với
    -- `item_id`, thứ khóa ngoại một cột không diễn đạt được), nên bộ đếm là thứ
    -- duy nhất chặn xóa hoặc gộp mất một quy cách mà chứng từ đang trỏ tới —
    -- xem `ItemVariantService.delete`. Đếm theo DÒNG, không theo chứng từ: hai
    -- dòng của cùng hóa đơn khai cùng quy cách là hai lần dùng.
    SELECT 'item_variants' AS entity_type, variant_id AS entity_id
    FROM sales_invoice_lines
    WHERE variant_id IS NOT NULL
),
bank_account_refs AS (
    SELECT 'company_bank_accounts' AS entity_type, account_id AS entity_id
    FROM (
        SELECT bank_account_id AS account_id FROM bank_vouchers
        UNION ALL
        SELECT counter_bank_account_id FROM bank_vouchers
        WHERE counter_bank_account_id IS NOT NULL
        UNION ALL
        -- Chiều `bank_account` trên dòng phiếu quỹ (lát 6G-1): cột
        -- `gl_postings.bank_account_id` cố ý KHÔNG có khóa ngoại, nên bộ đếm là
        -- thứ duy nhất chặn xóa một TK ngân hàng mà sổ đang trỏ tới — và
        -- `opening_balances.bank_account_id` thì CÓ khóa ngoại, nên lượt chuyển
        -- năm sau đổ nếu TK biến mất (review pre-landing H-B).
        SELECT bank_account_id FROM cash_voucher_lines
        WHERE bank_account_id IS NOT NULL
        UNION ALL
        -- Nhập sao kê cũng `record_use` (`statement_import`) nhưng nhánh này
        -- thiếu từ lát 6D, nên check ĐỎ ngay lượt nhập sao kê đầu tiên —
        -- bộ đếm nói 1, phía tham chiếu nói 0 (review pre-landing vòng 2 H-1).
        -- Lập luận H-B của lát này viện dẫn chính check ấy, nên nó phải đúng.
        SELECT bank_account_id FROM bank_statements
    ) refs
),
counted AS (
    SELECT entity_type, entity_id, COUNT(*) AS references_seen
    FROM (
        SELECT entity_type, entity_id FROM partner_refs
        UNION ALL
        SELECT entity_type, entity_id FROM bank_account_refs
        UNION ALL
        SELECT entity_type, entity_id FROM variant_refs
    ) all_refs
    GROUP BY 1, 2
)
SELECT COALESCE(u.entity_type, c.entity_type) AS entity_type,
       COALESCE(u.entity_id, c.entity_id)     AS entity_id,
       COALESCE(u.usage_count, 0)             AS usage_count,
       COALESCE(c.references_seen, 0)         AS counted_references
FROM master_data_usage u
FULL JOIN counted c
       ON c.entity_type = u.entity_type AND c.entity_id = u.entity_id
WHERE COALESCE(u.usage_count, 0) <> COALESCE(c.references_seen, 0)
