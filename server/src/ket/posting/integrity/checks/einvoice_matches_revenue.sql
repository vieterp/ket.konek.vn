-- SRS 07 §5 #4 ở dạng BẤT BIẾN: mỗi hóa đơn điện tử đã tiêu số phải có đúng phần
-- doanh thu tương ứng trên sổ tài chính của chính chứng từ sinh ra nó.
--
-- Tệp này viết ở 7G-3 và **CỐ Ý ĐỨNG NGOÀI `CHECKS`.** Luật của registry (xem
-- docstring `registry.py`): viết câu trước, đăng ký sau, và chỉ đăng ký khi nó
-- xanh trên dữ liệu ĐÚNG — một check kêu sai dạy người dùng bỏ qua mọi check còn
-- lại. `arap_matches_control` đứng ngoài **năm lát** trước khi vào, và
-- `opening_detail_matches_control` phải bị GỠ RA sau khi đã vào vì nó đỏ oan.
--
-- **Ba điều kiện phải đóng trước khi đăng ký**, cả ba là ca đẳng thức sai trên dữ
-- liệu ĐÚNG:
--
--  1. **Hóa đơn thay thế và điều chỉnh.** Một chứng từ bán có thể có NHIỀU hóa
--     đơn qua vòng đời sai sót (7F): tờ gốc `DA_THAY_THE`, tờ thay thế mang lại
--     toàn bộ tiền, và cả hai cùng trỏ về... hai chứng từ khác nhau — nhưng hai
--     `kind` ĐIỀU CHỈNH (7F-2a) chỉ mang **phần chênh**, nên tờ hóa đơn điều
--     chỉnh đối chiếu được với chứng từ chênh của nó chứ không với hóa đơn gốc.
--     Câu hỏi chưa trả lời: một tờ `DA_THAY_THE` có còn phải khớp doanh thu
--     không, khi chứng từ của nó vẫn ghi sổ nguyên vẹn? Nếu có thì tổng doanh thu
--     bị đếm hai lần ở tầng báo cáo; nếu không thì phải loại nó, và phải chứng
--     minh được rằng loại nó không mở một lỗ khác.
--
--  2. **`PHAT_HANH_LOI` giữ số.** Nhà cung cấp hoặc cơ quan thuế từ chối, số đã
--     cấp **không nhả ra** — nhưng chứng từ bán vẫn ghi sổ bình thường. Tờ hóa
--     đơn ấy có phải một nghĩa vụ đối chiếu không, hay nó là một số đã tiêu mà
--     chưa có tờ giấy nào? Hai câu trả lời dẫn tới hai câu SQL khác nhau.
--
--  3. **Bút toán tay gõ thẳng vào 511.** Chiều ngược của đẳng thức: doanh thu có
--     mà không hóa đơn nào. Hợp lệ (kết chuyển, điều chỉnh cuối kỳ, doanh thu
--     không thuộc diện lập hóa đơn), nên vế "mỗi đồng 511 phải có một tờ hóa đơn"
--     KHÔNG đăng ký được nếu không có cách phân biệt — và cách ấy hôm nay chưa có.
--
-- **Một khiếm khuyết thứ tư, thuộc về hạ tầng chứ không thuộc nghiệp vụ:** phép
-- nhận diện nhóm tài khoản doanh thu đi bằng **tiền tố số hiệu** (`511`/`512`),
-- trong khi hệ thống tài khoản là dữ liệu của gói cấu hình và người dùng thêm
-- được tài khoản con. Đường đúng là một **mục đích tài khoản** như `cash`/`bank`
-- đã có từ 6A. Một check chạy nền trên một phép phân loại đoán được là một check
-- sẽ đỏ trên một bản cài nào đó mà không ai đoán ra vì sao.
--
-- Hình dạng câu hỏi ở đây là bản HẸP NHẤT còn có nghĩa: chỉ những tờ ở
-- `DA_PHAT_HANH` / `DA_GUI` (đã phát hành thành công, chưa bị thay thế hay điều
-- chỉnh), đối chiếu tổng tiền thanh toán với doanh thu + thuế của cùng chứng từ.
-- Ngay cả bản hẹp ấy cũng chưa được chạy trên dữ liệu thật đủ rộng để đăng ký.
SELECT e.id                                 AS einvoice_id,
       e.invoice_no,
       e.branch_id,
       v.voucher_no,
       COALESCE(payable.amount, 0)          AS invoice_amount,
       COALESCE(revenue.amount, 0) + COALESCE(vat.amount, 0) AS booked_amount,
       COALESCE(payable.amount, 0)
           - COALESCE(revenue.amount, 0)
           - COALESCE(vat.amount, 0)        AS variance
FROM einvoices e
JOIN vouchers v ON v.id = e.source_voucher_id
JOIN sales_invoices si ON si.id = e.source_voucher_id
LEFT JOIN LATERAL (
    SELECT SUM(gp.debit - gp.credit) AS amount
    FROM gl_postings gp
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.account_id = si.receivable_account_id
      AND gp.ledger = 0
) AS payable ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    JOIN chart_of_accounts coa ON coa.id = gp.account_id
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.ledger = 0
      AND (coa.code LIKE '511%' OR coa.code LIKE '512%')
) AS revenue ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    JOIN chart_of_accounts coa ON coa.id = gp.account_id
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.ledger = 0
      AND coa.code LIKE '3331%'
) AS vat ON TRUE
WHERE e.status IN (3, 4)
  AND COALESCE(payable.amount, 0)
      - COALESCE(revenue.amount, 0)
      - COALESCE(vat.amount, 0) <> 0
