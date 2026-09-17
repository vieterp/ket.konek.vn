-- Dataset `einvoice_revenue_check`: đối chiếu tờ hóa đơn đã phát hành với doanh
-- thu ghi sổ của chính chứng từ sinh ra nó (SRS 07 §5 #4) — lát 7G-3.
--
-- **Tờ giấy này tồn tại vì các dòng LỆCH, nên nó không được lọc chúng đi.** Một
-- báo cáo đối chiếu chỉ in dòng khớp là một tờ giấy luôn luôn đẹp; cột `variance`
-- ở đây là cột người đọc tìm, và `:mismatch_only` cho phép thu về đúng phần ấy khi
-- muốn — mặc định KHÔNG lọc, vì "không lệch đồng nào" cũng là một khẳng định cần
-- nhìn thấy để tin.
--
-- **Hai vế đo hai thứ khác nhau, có chủ đích.** Vế trái là **tổng tiền thanh
-- toán** của tờ hóa đơn (phần ghi Nợ TK phải thu: tiền hàng + thuế). Vế phải là
-- **doanh thu** (phần ghi Có nhóm TK 511/512, KHÔNG gồm thuế đầu ra ở 3331). Nên
-- `variance = total − revenue − vat` chứ không phải `total − revenue`: bỏ vế thuế
-- ra ngoài thì mọi hóa đơn có thuế đều lệch đúng phần thuế, và một tờ đối chiếu
-- mà **mọi dòng đều đỏ** thì không ai đọc nó nữa.
--
-- Nhóm tài khoản nhận diện bằng **tiền tố số hiệu** (`511`/`512` cho doanh thu,
-- `3331` cho thuế GTGT đầu ra). Đây là chỗ duy nhất trong cả bộ dataset làm vậy,
-- và nó là một khiếm khuyết đã biết chứ không phải một lựa chọn: hệ thống tài
-- khoản là **dữ liệu của gói cấu hình** (TT99 / TT133, và người dùng thêm tài
-- khoản con), nên một doanh nghiệp đánh số doanh thu khác quy ước sẽ làm tờ này
-- nói dối. Đường đúng là một **mục đích tài khoản** (`account purpose`) như
-- `cash`/`bank` đã có từ 6A; mở nó cho doanh thu là việc của phase 9, khi bảng kê
-- thuế đầu ra cần đúng phép phân loại ấy. Ghi rõ ở đây để lát ấy nhặt.
--
-- **Chỉ hóa đơn đã tiêu số** có mặt: tờ chưa cấp số chưa là một chứng từ nào, và
-- đối chiếu nó với sổ là đối chiếu một thứ chưa tồn tại.
--
-- **Tờ ĐÃ BỊ THAY THẾ (`DA_THAY_THE`) đứng ngoài, và đó là một lỗi cộng đôi mà
-- vòng review bắt được.** `_supersede` dùng lại **chính** `source_voucher_id` cho
-- tờ thay thế, nên hai tờ đọc cùng một bộ `gl_postings`: mỗi dòng tự nó nói chênh
-- 0 — tức tờ giấy xanh trót lọt — trong khi cột doanh thu ở dòng TỔNG cộng gấp
-- đôi. Hỏng theo kiểu tệ nhất: không dòng nào đỏ, và con số tổng thì sai.
--
-- Tờ ĐÃ ĐIỀU CHỈNH (`DA_DIEU_CHINH`) thì **ở lại**, và ranh giới ấy không tùy
-- tiện: hai `kind` điều chỉnh (7F-2a) mang phần chênh trên một chứng từ KHÁC, nên
-- tờ gốc vẫn đối chiếu được với chứng từ của chính nó. Loại cả hai là bỏ mất một
-- dòng đối chiếu hợp lệ.
--
-- **Chứng từ CHƯA GHI SỔ là một dòng LỆCH, không phải một dòng sạch.** Cả ba vế
-- tiền đều đọc `gl_postings`, nên một tờ hóa đơn đã phát hành trên chứng từ còn
-- nháp cho ra `0 − 0 − 0 = 0` và tờ giấy báo "khớp" — đúng ca mà SRS 07 §5 #4
-- tồn tại để bắt, và `guards.py` ghi rõ trạng thái ấy hợp lệ về mặt vòng đời
-- (FR-EIV-011). Cột `posting_state` nói ra tình trạng, và `:mismatch_only` nhận
-- những dòng ấy kể cả khi chênh bằng 0.
--
-- Tham số: :from_date, :to_date (khoảng ngày hóa đơn), :ledger, :branch_ids,
-- :mismatch_only.
SELECT e.branch_id,
       COALESCE(e.invoice_date, v.document_date) AS period_date,
       e.invoice_no,
       e.invoice_date,
       form.form_no,
       form.code                            AS form_code,
       v.voucher_no                         AS source_voucher_no,
       customer.code                        AS customer_code,
       COALESCE(customer.name, 'Chưa khai khách hàng') AS customer_name,
       CASE e.status
           WHEN 1 THEN 'Đang phát hành'
           WHEN 2 THEN 'Phát hành lỗi'
           WHEN 3 THEN 'Đã phát hành'
           WHEN 4 THEN 'Đã gửi người mua'
           WHEN 6 THEN 'Đã điều chỉnh'
           WHEN 7 THEN 'Đã hủy'
           ELSE 'Trạng thái ' || e.status::text
       END                                  AS status_label,
       COALESCE(payable.amount, 0)          AS invoice_amount,
       COALESCE(revenue.amount, 0)          AS revenue_amount,
       COALESCE(vat.amount, 0)              AS vat_amount,
       COALESCE(payable.amount, 0)
           - COALESCE(revenue.amount, 0)
           - COALESCE(vat.amount, 0)        AS variance,
       CASE WHEN v.status = 2 THEN 'Đã ghi sổ' ELSE 'CHƯA ghi sổ' END AS posting_state
FROM einvoices e
JOIN vouchers v ON v.id = e.source_voucher_id
LEFT JOIN sales_invoices si ON si.id = e.source_voucher_id
LEFT JOIN invoice_forms form ON form.id = e.invoice_form_id
LEFT JOIN partners customer ON customer.id = si.customer_id
LEFT JOIN LATERAL (
    SELECT SUM(gp.debit - gp.credit) AS amount
    FROM gl_postings gp
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.account_id = si.receivable_account_id
      AND gp.ledger = :ledger
) AS payable ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    JOIN chart_of_accounts coa ON coa.id = gp.account_id
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.ledger = :ledger
      AND (coa.code LIKE '511%' OR coa.code LIKE '512%')
) AS revenue ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    JOIN chart_of_accounts coa ON coa.id = gp.account_id
    WHERE gp.voucher_id = e.source_voucher_id
      AND gp.ledger = :ledger
      AND coa.code LIKE '3331%'
) AS vat ON TRUE
WHERE e.invoice_no IS NOT NULL
  -- Xem docstring: tờ đã bị thay thế đọc chung chứng từ với tờ thay thế nó.
  AND e.status <> 5
  AND COALESCE(e.invoice_date, v.document_date) >= :from_date
  AND COALESCE(e.invoice_date, v.document_date) <= :to_date
  AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR e.branch_id = ANY(:branch_ids))
  AND (
      CAST(:mismatch_only AS BOOLEAN) IS NOT TRUE
      OR COALESCE(payable.amount, 0)
         - COALESCE(revenue.amount, 0)
         - COALESCE(vat.amount, 0) <> 0
      -- Chứng từ chưa ghi sổ: chênh bằng 0 vì cả hai vế đều rỗng, không vì
      -- chúng khớp nhau.
      OR v.status <> 2
  )
