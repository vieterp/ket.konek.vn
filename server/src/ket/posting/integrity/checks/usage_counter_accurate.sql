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
    ) refs
),
bank_account_refs AS (
    SELECT 'company_bank_accounts' AS entity_type, account_id AS entity_id
    FROM (
        SELECT bank_account_id AS account_id FROM bank_vouchers
        UNION ALL
        SELECT counter_bank_account_id FROM bank_vouchers
        WHERE counter_bank_account_id IS NOT NULL
    ) refs
),
counted AS (
    SELECT entity_type, entity_id, COUNT(*) AS references_seen
    FROM (
        SELECT entity_type, entity_id FROM partner_refs
        UNION ALL
        SELECT entity_type, entity_id FROM bank_account_refs
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
