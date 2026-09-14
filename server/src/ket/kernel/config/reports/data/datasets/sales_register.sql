-- Dataset `sales_register`: dòng hàng bán của chứng từ đã ghi sổ — nền của
-- SRS 06 §5.1 #1 (tổng hợp bán hàng, năm chiều gộp), #2 (sổ chi tiết bán hàng),
-- #3 (sổ chi tiết theo mã quy cách), #4 (sổ nhật ký bán hàng) và #5 (doanh số
-- theo thời gian). Chín định nghĩa báo cáo dùng CHUNG câu này, khác nhau ở
-- layout (chiều gộp) và ở tham số lọc — cùng lập luận của `purchase_register`:
-- engine gắn layout vào definition và **không** nhận `group_by` lúc chạy, mà mở
-- hợp đồng ấy để nhận chiều gộp động là đưa một identifier vào SQL trên một cỗ
-- máy đã ổn định từ phase 5.
--
-- **Tiền VND lấy từ `gl_postings`, không tự quy đổi lại.** Dòng hàng bán chỉ
-- mang cột nguyên tệ (`amount_fc`, `vat_amount_fc`, `discount_amount_fc`) —
-- không có cột VND nào. Nhân `amount_fc * exchange_rate` ở đây là dựng **cách
-- làm tròn thứ hai** song song với cách của posting engine, và hai cách ấy lệch
-- ở đồng cuối trên mọi hóa đơn ngoại tệ: báo cáo bán hàng sẽ không cộng ra đúng
-- số phát sinh TK 511 của chính nó. Đọc dòng phát sinh còn đóng ba thứ **bằng
-- cấu trúc**, y như ở `purchase_register`:
--
-- * **Sổ**: `vouchers` KHÔNG có cột `ledger` — sổ là thuộc tính của từng dòng
--   định khoản (phase 4), nên chiều sổ tài chính / quản trị chỉ đọc được ở đây.
-- * **Chỉ chứng từ đã ghi sổ**: `gl_postings` chỉ chứa chứng từ đã ghi sổ
--   (BR-RPT-01), nên chứng từ mới Cất không lọt vào báo cáo bằng *cấu trúc*,
--   không bằng một điều kiện ai đó phải nhớ viết.
-- * **Chiều phân tích** đã qua bước chuẩn hóa/tính lại của posting engine.
--
-- Số lượng và đơn giá thì ngược lại: chúng KHÔNG có trong `gl_postings` nên lấy
-- từ dòng hàng. Nối theo `source_line_id` **và** `account_id` để nhặt đúng phần
-- ghi vào tài khoản doanh thu: một dòng hàng còn sinh dòng thuế GTGT đầu ra trên
-- tài khoản khác, nối rộng hơn thì số lượng bị nhân bản và "tổng số lượng bán"
-- thành gấp đôi.
--
-- **`SUM(credit - debit)`, không phải `debit - credit`.** Doanh thu là số dư bên
-- Có; đây là chỗ duy nhất câu này lệch khuôn `purchase_register` (giá trị nhập
-- kho là số dư bên Nợ), và lệch vì chiều của tài khoản, không vì một lựa chọn
-- trình bày. Đảo hai vế ở đây cho ra một báo cáo bán hàng toàn số âm.
--
-- **Trả lại / giảm giá / điều chỉnh giảm mang dấu ÂM, và dấu ấy đến từ HAI
-- nguồn khác nhau.** Chỗ dễ sai nhất của cả dataset này: cột tiền lấy từ
-- `gl_postings` **đã có dấu**, cột lấy từ thân chứng từ thì **chưa**. Bút toán
-- của ba loại thuộc `REVERSING_KINDS` (2 hàng bán trả lại, 3 giảm giá hàng bán,
-- 6 điều chỉnh giảm) đã đảo chiều ở `posting_mapper`, nên `SUM(credit - debit)`
-- của chúng vốn đã âm; nhân thêm −1 ở đây là **đảo dấu hai lần** và phần ghi
-- giảm quay về dương, tức đúng con số phóng đại mà việc đảo dấu sinh ra để
-- tránh. Lát 7G-1 sập đúng bẫy này ở chiều mua và chỉ một bài kiểm bắt được.
-- Còn `quantity`, `l.amount_fc` và `l.discount_amount_fc` là giá trị khai trên
-- thân hóa đơn, luôn không âm (`totals_not_negative`), nên chúng — và chỉ chúng
-- — cần nhân dấu.
--
-- Nói ngắn: **sổ giữ dấu của nghiệp vụ; báo cáo không được ghi lại nó.**
--
-- `kind = 5` (điều chỉnh TĂNG) **không** thuộc tập đảo dấu: nó sinh một khoản nợ
-- mới đứng riêng và đi nhánh hóa đơn thường, đúng như `REVERSING_KINDS` của
-- `sales.models` phát biểu. Liệt kê ĐỦ bảy `kind` ở `kind_label` và để `ELSE`
-- lộ số thô, vì một `kind` tương lai dán nhãn sai trong khi nhánh dấu tính
-- ngược là hai chỗ nói hai điều về cùng một dòng.
--
-- **`amount + vat_amount` là tổng tiền thanh toán của dòng — ở đây nó ĐÚNG như
-- tên gọi**, khác `purchase_register` nơi cột cùng vị trí phải mang nhãn hẹp
-- hơn (`value_with_vat`, vì `goods.amount` đã gồm chi phí mua phân bổ). Hóa đơn
-- bán không có khoản chi phí phân bổ nào, và mapper ghi đúng hai cặp — Nợ 131 /
-- Có 511 và Nợ 131 / Có 33311 — nên tổng hai cột này bằng đúng phần ghi Nợ TK
-- phải thu của dòng.
--
-- **Chiết khấu thương mại là cột của THÂN chứng từ, không phải của sổ.** Mapper
-- ghi doanh thu đã **trừ** chiết khấu ngay trên dòng (SRS 06 §3.2 "trừ trực
-- tiếp trên hóa đơn"), nên không có cặp 521 riêng và `discount_amount_fc` không
-- đi vào bút toán nào. Cột ấy vì thế đọc được nhưng **không đối chiếu với tài
-- khoản nào** — nhãn layout nói rõ "nguyên tệ" và nó không bao giờ nằm trong
-- `totals`.
--
-- **Không có cột giá vốn, không có cột lãi/lỗ.** Nợ 632 / Có 156 đòi giá xuất
-- kho, mà giá xuất kho là việc của phase 8 — `sales_invoices.cogs_posted` ở lại
-- `false` cho tới lúc ấy. Một cột lãi gộp hôm nay đọc ra "lãi 100%": sai mà vẫn
-- ra số, đúng hình dạng lỗi mà lát 7G-1 đã hoãn sáu báo cáo để tránh.
--
-- **Tổng số lượng và cột nguyên tệ KHÔNG cộng tổng** (kỷ luật từ `ar_ap_aging`
-- của 7A): cộng USD với EUR ra một con số không có nghĩa, cộng "cái" với "thùng"
-- cũng vậy vì một vật tư bán bằng nhiều đơn vị quy đổi (3B-3).
--
-- *Hai giới hạn đã biết.* (1) Thuế GTGT đầu ra ở đây là thuế của **dòng hàng**
-- (`l.vat_account_id`); bảng kê thuế đầu ra của phase 9 đọc `gl_postings` trực
-- tiếp và thấy đủ. (2) Chiều gộp "theo đơn đặt hàng / hợp đồng" đọc được
-- `sales_invoice_lines.order_id`/`contract_id` (chiều phân tích đã có) nhưng
-- chưa phân hệ nào làm chủ hai danh mục ấy — cùng món nợ mà 7G-1 ghi cho chiều
-- mua, không phải thứ bỏ quên.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids (lớp bọc lọc),
-- :customer_id, :item_id, :variant_id, :kind, :salesperson_id, :project_id.
SELECT
    revenue.ledger,
    v.branch_id,
    revenue.posting_date,
    v.document_date,
    v.voucher_no,
    -- Chiều gộp "theo thời gian" của SRS 06 §5.1 #5. Gộp theo THÁNG GHI SỔ, cùng
    -- mốc với mọi cột tiền ở dưới: gộp theo ngày chứng từ sẽ cho một tổng tháng
    -- không khớp phát sinh TK 511 của chính tháng ấy.
    to_char(revenue.posting_date, 'YYYY-MM')            AS period_month,
    si.kind,
    CASE si.kind
        WHEN 0 THEN 'Bán hàng hóa'
        WHEN 1 THEN 'Bán dịch vụ'
        WHEN 2 THEN 'Hàng bán bị trả lại'
        WHEN 3 THEN 'Giảm giá hàng bán'
        WHEN 4 THEN 'Bán qua đại lý'
        WHEN 5 THEN 'Điều chỉnh tăng'
        WHEN 6 THEN 'Điều chỉnh giảm'
        ELSE 'Loại chứng từ ' || si.kind::text
    END                                                 AS kind_label,
    customer.code                                       AS customer_code,
    customer.name                                       AS customer_name,
    -- Chứng từ không khai nhân viên bán / không thuộc công trình / không khai
    -- vật tư / khách hàng chưa khai tỉnh gộp thành một nhóm có TÊN, không phải
    -- một nhóm rỗng: "chưa khai" là một câu trả lời, còn ô trống trên tiêu đề
    -- nhóm đọc như lỗi hiển thị. Mã thì để trống thật — không bịa một mã không
    -- có trong danh mục.
    COALESCE(customer.province, 'Không khai địa phương') AS province_name,
    branch.code                                         AS branch_code,
    branch.name                                         AS branch_name,
    salesperson.code                                    AS salesperson_code,
    COALESCE(salesperson.name, 'Chưa khai nhân viên bán') AS salesperson_name,
    project.code                                        AS project_code,
    COALESCE(project.name, 'Không thuộc công trình')     AS project_name,
    item.code                                           AS item_code,
    COALESCE(item.name, l.description, 'Không khai vật tư') AS item_name,
    variant.code                                        AS variant_code,
    COALESCE(variant.name, 'Không khai quy cách')        AS variant_name,
    unit.name                                           AS unit_name,
    COALESCE(l.description, v.description)              AS description,
    coa.code                                            AS account_code,
    -- Dấu nhân cho ĐÚNG những cột đến từ thân chứng từ — xem docstring đầu tệp
    -- về chuyện đảo dấu hai lần. Đơn giá không đảo dấu: nó là giá, không phải
    -- lượng.
    l.quantity * nature.factor                          AS quantity,
    l.unit_price_fc                                     AS unit_price_fc,
    l.discount_amount_fc * nature.factor                AS discount_amount_fc,
    l.amount_fc * nature.factor                         AS goods_amount_fc,
    -- Bốn cột dưới đây lấy từ `gl_postings`: dấu đã nằm trong `credit - debit`.
    revenue.amount_fc                                   AS amount_fc,
    revenue.amount                                      AS amount,
    COALESCE(vat.amount, 0)                             AS vat_amount,
    revenue.amount + COALESCE(vat.amount, 0)            AS amount_with_vat
FROM sales_invoice_lines l
JOIN sales_invoices si ON si.id = l.voucher_id
JOIN vouchers v ON v.id = si.id
CROSS JOIN LATERAL (
    -- `REVERSING_KINDS` của `sales.models`: trả lại (2), giảm giá (3), điều
    -- chỉnh giảm (6). Điều chỉnh TĂNG (5) đi nhánh hóa đơn thường.
    SELECT CASE WHEN si.kind IN (2, 3, 6) THEN -1 ELSE 1 END AS factor
) AS nature
-- Phần ghi vào tài khoản doanh thu. `SUM` vì một dòng hàng có thể tách thành
-- nhiều dòng phát sinh trên cùng tài khoản (chiều phân tích có thể khác nhau) —
-- lấy `LIMIT 1` ở đây là bỏ tiền đi im lặng. `INNER JOIN` chứ không `LEFT`:
-- dòng hàng chưa ghi sổ (hoặc ngoài khoảng ngày, hoặc thuộc sổ khác) KHÔNG có
-- mặt trong báo cáo — đó chính là phép lọc "chỉ chứng từ đã ghi sổ".
JOIN LATERAL (
    SELECT SUM(gp.credit_fc - gp.debit_fc) AS amount_fc,
           SUM(gp.credit - gp.debit)       AS amount,
           MIN(gp.ledger)                  AS ledger,
           MIN(gp.posting_date)            AS posting_date
    FROM gl_postings gp
    WHERE gp.source_line_id = l.id
      AND gp.account_id = l.account_id
      AND gp.ledger = :ledger
      AND gp.posting_date >= :from_date
      AND gp.posting_date <= :to_date
) AS revenue ON revenue.amount IS NOT NULL
LEFT JOIN LATERAL (
    SELECT SUM(gp.credit - gp.debit) AS amount
    FROM gl_postings gp
    WHERE gp.source_line_id = l.id
      AND l.vat_account_id IS NOT NULL
      AND gp.account_id = l.vat_account_id
      AND gp.ledger = :ledger
      -- Cùng cửa sổ ngày với LATERAL doanh thu. Hôm nay mọi dòng của một chứng
      -- từ cùng `posting_date` nên điều kiện này không đổi kết quả; thiếu nó là
      -- để hai chân của cùng một phép cộng đứng trên hai phạm vi khác nhau.
      AND gp.posting_date >= :from_date
      AND gp.posting_date <= :to_date
) AS vat ON TRUE
JOIN chart_of_accounts coa ON coa.id = l.account_id
JOIN partners customer ON customer.id = si.customer_id
-- `branches` là danh mục, không phải dữ liệu phát sinh, nên nó không đứng sau
-- RLS (xem `0001_core_platform`): chiều gộp "theo đơn vị" của SRS 06 §5.1 #1 là
-- một phép nối thẳng. Phạm vi chi nhánh của chính báo cáo vẫn do lớp bọc
-- `compose_scoped_query` và RLS của `vouchers` canh.
JOIN branches branch ON branch.id = v.branch_id
LEFT JOIN employees salesperson ON salesperson.id = si.salesperson_id
LEFT JOIN projects project ON project.id = l.project_id
LEFT JOIN items item ON item.id = l.item_id
LEFT JOIN item_variants variant ON variant.id = l.variant_id
LEFT JOIN units_of_measure unit ON unit.id = l.unit_id
WHERE (CAST(:customer_id AS INTEGER) IS NULL OR si.customer_id = :customer_id)
  AND (CAST(:item_id AS INTEGER) IS NULL OR l.item_id = :item_id)
  AND (CAST(:variant_id AS INTEGER) IS NULL OR l.variant_id = :variant_id)
  AND (CAST(:kind AS INTEGER) IS NULL OR si.kind = :kind)
  AND (CAST(:salesperson_id AS INTEGER) IS NULL OR si.salesperson_id = :salesperson_id)
  AND (CAST(:project_id AS INTEGER) IS NULL OR l.project_id = :project_id)
