-- Dataset `ar_open_items_by_item`: công nợ phải thu còn treo, xem theo DÒNG HÀNG
-- của hóa đơn sinh ra khoản nợ ấy (SRS 06 §5.2 #4) — lát 7G-2b.
--
-- **Sổ KHÔNG ghi tiền nợ theo dòng hàng, và báo cáo này không giả vờ ngược lại.**
-- Tiền khách trả đối trừ theo **khoản** (`ar_ap_ledger.id`), không theo dòng hóa
-- đơn: không bảng nào nói dòng "Hàng A" của hóa đơn HD-001 đã thu bao nhiêu.
-- Chia phần còn nợ cho các dòng theo tỷ lệ giá trị là dựng một phép chia **mà
-- không sổ nào ghi** — cùng họ với "cách làm tròn thứ hai" mà 7G-1 và 7G-2a đã
-- hai lần từ chối, và nguy hiểm hơn ở chỗ kết quả vẫn cộng ra đúng tổng nên
-- không có gì đối chiếu ra.
--
-- Nên **user chốt 2026-09-14: không phân bổ.** Mỗi dòng in giá trị và số lượng
-- của CHÍNH nó; cột `remaining` là phần còn nợ của **cả hóa đơn**, lặp lại trên
-- mọi dòng của hóa đơn ấy và mang nhãn nói rõ điều đó. Hệ quả bắt buộc:
-- **không layout nào được cộng tổng `remaining` trên dataset này** — cộng nó là
-- nhân khoản nợ lên đúng số dòng hàng. `test_no_layout_totals_the_invoice_debt`
-- là cổng chặn.
--
-- **Chỉ khoản sinh từ HÓA ĐƠN BÁN** (`target_kind = 0`). Nợ phải thu ghi tay
-- bằng bút toán (3), khoản ta trả trước người bán (6) và nợ mang sang từ
-- `opening_balance_invoices` (2) **không có dòng hàng nào** — chúng vắng mặt ở
-- đây theo chủ đích, không phải bỏ sót, và tổng của tờ này vì thế NHỎ HƠN tổng
-- công nợ phải thu. Ai cần con số đối chiếu TK 131 thì đọc `ar_ap_open_items`.
--
-- **Không nhân dấu ở đâu cả, và đó là một phát hiện chứ không phải một lựa
-- chọn.** `sales.service.sync_after_post` chỉ ghi dòng sổ phụ cho hóa đơn KHÔNG
-- thuộc `REVERSING_KINDS`; trả lại / giảm giá / điều chỉnh giảm đi đường đối trừ
-- và không sinh khoản nợ nào. Nên mọi khoản tới được đây đều mang chiều thuận,
-- và một `CASE ... THEN -1` ở đây sẽ là mã chết — tệ hơn, là mã chết trông như
-- một phép canh.
--
-- **Tiền VND của dòng đọc từ `gl_postings`**, đúng kỷ luật `purchase_register`
-- (7G-1) và `sales_register` (7G-2a): dòng hàng bán chỉ có cột nguyên tệ, nên
-- nhân lại tỷ giá là dựng cách làm tròn thứ hai và tổng của báo cáo thôi khớp
-- phát sinh của chính tài khoản nó nói về. Nối theo `source_line_id` **và**
-- `account_id` để số lượng không nhân bản lên dòng thuế.
--
-- **LATERAL doanh thu KHÔNG cắt theo `:from_date`.** Công nợ là một con số
-- TỒN, không phải phát sinh trong kỳ: khoản nợ vào báo cáo theo `document_date
-- <= :to_date`, nên cắt dưới ở phần doanh thu sẽ làm dòng hàng của một hóa đơn
-- cũ biến mất trong khi khoản nợ của nó vẫn hiện — một hóa đơn còn nợ mà không
-- có dòng hàng nào.
--
-- Tham số: :from_date (không dùng — thuộc bộ chuẩn engine luôn truyền),
-- :to_date (mốc chốt số), :ledger, :branch_ids, :customer_id, :item_id,
-- :open_only.
WITH settled_as_of AS (
    -- #include: settled_as_of.sql
)
SELECT d.branch_id,
       d.ledger,
       customer.code                        AS partner_code,
       customer.name                        AS partner_name,
       d.document_no,
       d.document_date,
       d.due_date,
       item.code                            AS item_code,
       -- Dòng không khai vật tư (bán dịch vụ, phí) gộp vào một nhóm CÓ TÊN:
       -- diễn giải của chính dòng nếu có, vì nó là thứ người lập chứng từ đã
       -- viết ra để mô tả thứ mình bán.
       COALESCE(item.name, l.description, 'Không khai vật tư') AS item_name,
       variant.code                         AS variant_code,
       unit.name                            AS unit_name,
       l.quantity,
       l.amount_fc                          AS goods_amount_fc,
       revenue.amount                       AS goods_amount,
       d.currency_code,
       -- Ba cột của cả HÓA ĐƠN, lặp trên mọi dòng của nó — xem docstring đầu
       -- tệp về việc không layout nào được cộng tổng chúng.
       d.amount                             AS invoice_amount,
       COALESCE(sa.settled, 0)              AS invoice_settled,
       d.amount - COALESCE(sa.settled, 0)   AS invoice_remaining
FROM ar_ap_ledger d
JOIN sales_invoice_lines l ON l.voucher_id = d.document_id
JOIN sales_invoices si ON si.id = l.voucher_id
-- Phần ghi vào tài khoản doanh thu của chính dòng hàng. `SUM` vì một dòng hàng
-- tách được thành nhiều dòng phát sinh trên cùng tài khoản (chiều phân tích có
-- thể khác nhau) — `LIMIT 1` ở đây là bỏ tiền đi im lặng.
JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    WHERE gp.source_line_id = l.id
      AND gp.account_id = l.account_id
      AND gp.ledger = :ledger
      AND gp.posting_date <= :to_date
) AS revenue ON revenue.amount IS NOT NULL
LEFT JOIN settled_as_of sa
       ON sa.target_kind = d.target_kind
      AND sa.target_id = d.id
-- Khoản sinh từ hóa đơn bán luôn treo lên một khách hàng (`partner_kind` = 0),
-- nên phép nối này không cần điều kiện loại đối tác như `ar_ap_open_items`:
-- `partner_kind = 2` (nhân viên) không đến được đây.
LEFT JOIN partners customer ON customer.id = d.partner_id
LEFT JOIN items item ON item.id = l.item_id
LEFT JOIN item_variants variant ON variant.id = l.variant_id
LEFT JOIN units_of_measure unit ON unit.id = l.unit_id
WHERE d.target_kind = 0
  AND d.ledger = :ledger
  AND d.document_date <= :to_date
  AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR d.branch_id = ANY(:branch_ids))
  AND (CAST(:customer_id AS INTEGER) IS NULL OR si.customer_id = :customer_id)
  AND (CAST(:item_id AS INTEGER) IS NULL OR l.item_id = :item_id)
  -- Đóng theo NGUYÊN TỆ, cùng cơ sở với `ar_ap_open_items`: phần chênh VND do
  -- làm tròn khi thu tiền đi vào 515/635, nó không phải phần còn nợ.
  AND (
      CAST(:open_only AS BOOLEAN) IS NOT TRUE
      OR d.amount_fc > COALESCE(sa.settled_fc, 0)
  )
