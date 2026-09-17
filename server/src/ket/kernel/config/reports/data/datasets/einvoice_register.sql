-- Dataset `einvoice_register`: sổ hóa đơn điện tử — mỗi tờ hóa đơn một dòng, kèm
-- trạng thái vòng đời, mã cơ quan thuế và tiền của chính nó. Nền của SRS 07 §5 #1
-- (bảng kê hóa đơn đã phát hành) và #3 (danh sách theo trạng thái) — lát 7G-3.
--
-- Hai báo cáo, **một** dataset: chúng khác nhau đúng một tham số ghim, đúng khuôn
-- tham số chiều của `ar_ap_open_items` và tham số tình-trạng-hạn của 7G-2b. Engine
-- gắn layout vào definition và không nhận điều kiện lọc lúc chạy, nên phép chia là
-- một tham số chứ không phải hai tệp SQL.
--
-- (Viết tên tham số của dataset KHÁC mà kèm dấu hai chấm là một cái bẫy:
-- `sql_placeholders` quét cả phần chú thích — nó không phân tích SQL, có chủ đích,
-- vì nó đứng sau `assert_placeholders_allowed` tức một phép canh an ninh — nên một
-- ví dụ trong văn xuôi bị tính thành tham số chưa khai và cổng manifest đỏ.)
--
-- **Hóa đơn không thuộc sổ nào** — nó là một tờ giấy pháp lý, không phải một dòng
-- định khoản. Nhưng cột tiền bên dưới ĐỌC sổ, và sổ hai lớp của hệ (tài chính /
-- quản trị) có phát sinh ở cả hai, nên không chọn một sổ là cộng đôi tiền của mọi
-- tờ hóa đơn. Chọn bằng `:ledger` chứ không bằng hằng `0` viết thẳng: cơ chế
-- `ledger_scope` của definition đã làm đúng việc ghim ấy từ phase 5, và một hằng
-- trần trong SQL là chỗ thứ hai trả lời cùng câu hỏi. Ba định nghĩa của lát này
-- ghim `financial` — đó là sổ mà tờ hóa đơn đối chiếu được.
--
-- **Cửa sổ kỳ cắt theo `COALESCE(e.invoice_date, v.document_date)`, và đó là điều
-- kiện tồn tại của báo cáo #3.** Hóa đơn `CHUA_PHAT_HANH` **chưa có số và chưa có
-- ngày** (`invoice_no`/`invoice_date` đều NULL cho tới lúc cấp số — xem
-- `EInvoiceStatus`), mà #3 tồn tại chính để liệt kê nhóm ấy. Cắt thẳng theo
-- `invoice_date` vì thế **loại sạch** đúng nhóm tờ giấy sinh ra để cho thấy — cùng
-- hình dạng lỗi NULL mà 7G-1 đã trả giá ở khoản ứng trước đầu kỳ, nơi
-- `advance_has_no_invoice_ref` buộc bỏ trống ngày hóa đơn và một điều kiện ngày
-- ngây thơ xóa cả họ khoản ấy khỏi báo cáo. Hóa đơn chưa cấp số vẫn thuộc kỳ của
-- chứng từ sinh ra nó.
--
-- **Tiền VND đọc từ `gl_postings`, không nhân lại tỷ giá** — kỷ luật
-- `purchase_register` (7G-1) và `sales_register` (7G-2a). `sales_invoices` chỉ có
-- cột nguyên tệ, nên nhân `total_fc * exchange_rate` ở đây là dựng **cách làm tròn
-- thứ hai** song song với cách của posting engine, và bảng kê hóa đơn thôi cộng ra
-- đúng số phát sinh của chính tài khoản nó nói về.
--
-- Đọc phần ghi vào **TK phải thu của chính chứng từ** (`si.receivable_account_id`,
-- lưu thật trên thân — không tra lại gói cấu hình), vì đó là vế mang **tổng tiền
-- thanh toán** của tờ hóa đơn: tiền hàng + thuế. Cộng vế doanh thu rồi cộng thêm
-- vế thuế là dựng lại cùng con số bằng hai phép cộng thay vì một.
--
-- `SUM(debit - credit)`: khoản phải thu là số dư bên Nợ. Chứng từ ghi giảm (trả
-- lại, giảm giá, điều chỉnh giảm) đã đảo chiều ở `posting_mapper`, nên dòng của
-- chúng vốn đã âm — **không nhân dấu lần nữa ở đây**. Đó là bẫy đảo dấu hai lần mà
-- 7G-1 sập và 7G-2a né được nhờ nó.
--
-- **Hóa đơn ĐẦU VÀO không có mặt**, và không phải vì một phép lọc: chúng ở bảng
-- riêng `inbound_einvoices` (lát 7F-2b), không bao giờ vào `einvoices`.
--
-- **`:issued_only` hỏi "tờ này ĐÃ CÓ SỐ chưa", không hỏi trạng thái.** Một tờ hóa
-- đơn tồn tại với tư cách chứng từ kể từ lúc nó mang số, và số ấy không nhả ra
-- nữa — kể cả `PHAT_HANH_LOI` giữ số, kể cả `DA_HUY` (BR-INV-04).
--
-- Bản đầu của lát 7G-3 viết ngưỡng ấy thành `status >= 1`, và vòng review bắt
-- được: với ký hiệu khai `provider_code` (nhà cung cấp cấp số — 7E-2), tờ
-- `DANG_PHAT_HANH` **chưa có số**, số về ở lượt xác nhận. Ngưỡng theo trạng thái
-- vì thế kéo một dòng số-rỗng vào "bảng kê hóa đơn đã phát hành" — và bài kiểm của
-- chính lát này khẳng định điều đó không xảy ra, tức bộ kiểm đang phát biểu một
-- bất biến mà mã không giữ. `invoice_no IS NOT NULL` đúng cho **cả hai** đường
-- đánh số, và nó là chính câu hỏi người đọc bảng kê đang hỏi.
--
-- Liệt kê tay vài trạng thái vào `fixed_params` thay cho một phép hỏi thì lát sau
-- thêm một trạng thái là bảng kê im lặng bỏ sót nó — cùng lý do.
--
-- Tham số: :from_date, :to_date (khoảng ngày hóa đơn — xem trên), :ledger,
-- :branch_ids, :issued_only, :status, :invoice_form_id, :customer_id.
SELECT e.branch_id,
       e.status,
       -- Trạng thái nói bằng tiếng người. `ELSE` lộ số thô thay vì đoán: một giá
       -- trị mới của `EInvoiceStatus` mà dán nhãn sẵn là một nhãn nói dối, còn
       -- một con số trần thì người đọc biết ngay là chưa ai đặt tên cho nó.
       CASE e.status
           WHEN 0 THEN 'Chưa phát hành'
           WHEN 1 THEN 'Đang phát hành'
           WHEN 2 THEN 'Phát hành lỗi'
           WHEN 3 THEN 'Đã phát hành'
           WHEN 4 THEN 'Đã gửi người mua'
           WHEN 5 THEN 'Đã thay thế'
           WHEN 6 THEN 'Đã điều chỉnh'
           WHEN 7 THEN 'Đã hủy'
           ELSE 'Trạng thái ' || e.status::text
       END                                  AS status_label,
       form.form_no                         AS form_no,
       form.code                            AS form_code,
       COALESCE(form.name, 'Chưa khai mẫu hóa đơn') AS form_name,
       e.invoice_no,
       e.invoice_date,
       COALESCE(e.invoice_date, v.document_date) AS period_date,
       v.voucher_no                         AS source_voucher_no,
       v.document_date                      AS source_document_date,
       customer.code                        AS customer_code,
       COALESCE(customer.name, 'Chưa khai khách hàng') AS customer_name,
       customer.tax_code                    AS customer_tax_code,
       e.tax_authority_code,
       e.lookup_code,
       -- Hai cạnh của vòng đời sai sót (7F): tờ nào thay thế / điều chỉnh tờ nào.
       -- Cột rỗng là câu trả lời "không phải hóa đơn xử lý sai sót", không phải
       -- một ô thiếu dữ liệu.
       replaced.invoice_no                  AS replaces_invoice_no,
       adjusted.invoice_no                  AS adjusts_invoice_no,
       v.currency_code,
       si.total_fc,
       payable.amount                       AS total_amount
FROM einvoices e
JOIN vouchers v ON v.id = e.source_voucher_id
LEFT JOIN sales_invoices si ON si.id = e.source_voucher_id
LEFT JOIN invoice_forms form ON form.id = e.invoice_form_id
LEFT JOIN partners customer ON customer.id = si.customer_id
LEFT JOIN einvoices replaced ON replaced.id = e.replaces_invoice_id
LEFT JOIN einvoices adjusted ON adjusted.id = e.adjusts_invoice_id
-- Tổng tiền thanh toán của tờ hóa đơn, ĐỌC TỪ SỔ. `LEFT JOIN LATERAL` chứ không
-- `JOIN`: hóa đơn `CHUA_PHAT_HANH` của một chứng từ còn nháp chưa có dòng phát
-- sinh nào, và #3 phải liệt kê được nó — một `INNER JOIN` ở đây làm nhóm ấy biến
-- mất y như một phép cắt ngày ngây thơ.
LEFT JOIN LATERAL (
    SELECT SUM(gp.debit - gp.credit) AS amount
    FROM gl_postings gp
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.account_id = si.receivable_account_id
      AND gp.ledger = :ledger
) AS payable ON TRUE
WHERE COALESCE(e.invoice_date, v.document_date) >= :from_date
  AND COALESCE(e.invoice_date, v.document_date) <= :to_date
  AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR e.branch_id = ANY(:branch_ids))
  AND (CAST(:issued_only AS BOOLEAN) IS NOT TRUE OR e.invoice_no IS NOT NULL)
  AND (CAST(:status AS INTEGER) IS NULL OR e.status = :status)
  AND (CAST(:invoice_form_id AS INTEGER) IS NULL OR e.invoice_form_id = :invoice_form_id)
  AND (CAST(:customer_id AS INTEGER) IS NULL OR si.customer_id = :customer_id)
