-- Dataset `purchase_register`: dòng hàng mua của chứng từ đã ghi sổ — nền của
-- SRS 05 §5 #1 (tổng hợp mua hàng, bốn chiều gộp), #2 (sổ chi tiết mua hàng) và
-- #3 (sổ nhật ký mua hàng). Sáu định nghĩa báo cáo dùng CHUNG câu này, khác
-- nhau ở layout (chiều gộp) và ở tham số lọc; chia thành sáu tệp SQL là nhân
-- bản sáu lần cùng một phép cộng tiền mua.
--
-- **Tiền VND lấy từ `gl_postings`, không tự quy đổi lại.** Dòng hàng chỉ mang
-- cột nguyên tệ (`amount_fc`, `vat_amount_fc`, `landed_cost_fc`) — không có cột
-- VND nào. Nhân `amount_fc * exchange_rate` ở đây là dựng **cách làm tròn thứ
-- hai** song song với cách của posting engine, và hai cách ấy lệch nhau ở đồng
-- cuối trên mọi hóa đơn ngoại tệ: báo cáo mua hàng sẽ không cộng ra đúng số
-- phát sinh TK 152/156/642 của chính nó. Đọc dòng phát sinh đóng thêm ba thứ:
--
-- * **Sổ**: `vouchers` KHÔNG có cột `ledger` — sổ là thuộc tính của từng dòng
--   định khoản (phase 4), nên chiều sổ tài chính / quản trị chỉ đọc được ở đây.
-- * **Chỉ chứng từ đã ghi sổ**: `gl_postings` chỉ chứa chứng từ đã ghi sổ
--   (BR-RPT-01), nên chứng từ mới Cất không lọt vào báo cáo bằng *cấu trúc*,
--   không bằng một điều kiện ai đó phải nhớ viết.
-- * **Chiều phân tích** đã qua bước chuẩn hóa/tính lại của posting engine.
--
-- Số lượng và đơn giá thì ngược lại: chúng KHÔNG có trong `gl_postings` nên
-- lấy từ dòng hàng. Nối theo `source_line_id` **và** `account_id` để nhặt đúng
-- phần ghi vào tài khoản hàng: một dòng hàng còn sinh dòng thuế GTGT trên tài
-- khoản khác, nối rộng hơn thì số lượng bị nhân bản và "tổng số lượng mua"
-- thành gấp đôi.
--
-- **`amount` là GIÁ TRỊ NHẬP KHO, gồm cả chi phí mua phân bổ — có chủ đích.**
-- Posting mapper ghi phần chi phí mua phân bổ vào **chính** `line.account_id`
-- với **chính** `source_line_id = line.id` (xem `landed_cost_lines`), nên nó
-- nằm trong cùng một tổng với tiền hàng và không tách ra được ở đây. Đó đúng là
-- BR-PUR-01: giá trị nhập kho = giá mua + chi phí mua phân bổ. Phần tiền hàng
-- thuần và phần chi phí hiện thành hai cột **nguyên tệ** riêng (`goods_amount_fc`,
-- `landed_cost_fc`) — hai cột ấy là giá trị ĐÃ LƯU trên dòng, không phải giá trị
-- quy đổi lại, nên chúng không mang thêm một phép làm tròn nào.
--
-- **Trả lại hàng mua mang dấu ÂM, và dấu ấy đến từ HAI nguồn khác nhau.**
-- SRS 05 §5 #1 nêu đích danh ba thứ trong MỘT báo cáo ("Mua hàng, trả lại,
-- giảm giá"), nên cột tiền ở đây là giá trị mua **thuần**.
--
-- Chỗ dễ sai nhất của cả dataset này: cột tiền lấy từ `gl_postings` **đã có
-- dấu**, cột lấy từ thân chứng từ thì **chưa**. Bút toán của chứng từ trả lại
-- đảo chiều (Có TK hàng / Nợ TK phải trả — xem `posting_mapper._reversed`), nên
-- `SUM(debit - credit)` của nó vốn đã âm; nhân thêm −1 ở đây là **đảo dấu hai
-- lần** và phần trả lại quay về dương, tức đúng con số phóng đại mà việc đảo
-- dấu sinh ra để tránh. Còn `quantity`, `l.amount_fc` và `l.landed_cost_fc` là
-- giá trị khai trên thân hóa đơn, luôn không âm (`totals_not_negative`), nên
-- chúng — và chỉ chúng — cần nhân dấu.
--
-- Nói ngắn: **sổ giữ dấu của nghiệp vụ; báo cáo không được ghi lại nó.**
--
-- *Hai giới hạn đã biết.* (1) Giảm giá hàng mua chưa có `kind` riêng
-- (`PurchaseInvoiceKind` có 0..4, không có DISCOUNT): chiết khấu thương mại hiện
-- nằm gộp trong `amount_fc` của dòng; khi phân hệ mua có loại chứng từ giảm giá,
-- nó chỉ cần vào nhánh đảo dấu bên dưới. (2) Thuế GTGT của **khoản chi phí mua**
-- gắn `source_line_id = cost.id` chứ không gắn dòng hàng (nó không phân bổ về
-- dòng), nên cột `vat_amount` ở đây là thuế của tiền hàng — bảng kê thuế đầu vào
-- của phase 9 đọc `gl_postings` trực tiếp và thấy đủ cả hai.
--
-- **Bộ lọc giao một phần FR-PUR-040 (MUST).** Yêu cầu ấy đòi mọi báo cáo mua
-- hàng lọc được theo tám chiều; ở đây có năm chiều mà dòng hàng **đã mang cột**:
-- thời gian, chi nhánh (lớp bọc), nhà cung cấp, vật tư, nhân viên mua, công
-- trình, cộng loại chứng từ. Ba chiều còn lại — **nhóm NCC** (cần đi cây
-- `partners`), **đơn mua hàng** và **hợp đồng** (`l.order_id`/`l.contract_id` có
-- cột nhưng chưa có phân hệ nào làm chủ chúng) — là nợ ghi rõ, không phải thứ bỏ
-- quên. User chốt 2026-09-13: giao năm chiều có sẵn cột, hoãn ba chiều cần join.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids (lớp bọc lọc),
-- :vendor_id, :item_id, :kind, :buyer_id, :project_id.
SELECT
    goods.ledger,
    v.branch_id,
    goods.posting_date,
    v.document_date,
    v.voucher_no,
    pi.kind,
    -- Liệt kê ĐỦ, `ELSE` lộ số thô: `ELSE 'Trả lại hàng mua'` của bản đầu sẽ
    -- dán nhãn "trả lại" cho một `kind` tương lai (giảm giá hàng mua) mà nhánh
    -- đảo dấu bên dưới lại tính dương — hai chỗ nói hai điều về cùng một dòng.
    CASE pi.kind
        WHEN 0 THEN 'Mua hàng nhập kho'
        WHEN 1 THEN 'Mua dịch vụ'
        WHEN 2 THEN 'Mua tài sản'
        WHEN 3 THEN 'Hàng đang đi đường'
        WHEN 4 THEN 'Trả lại hàng mua'
        ELSE 'Loại chứng từ ' || pi.kind::text
    END                                              AS kind_label,
    vendor.code                                      AS vendor_code,
    vendor.name                                      AS vendor_name,
    -- Chứng từ không khai người mua / không thuộc công trình / không khai vật tư
    -- gộp thành một nhóm có TÊN, không phải một nhóm rỗng: "chưa khai" là một
    -- câu trả lời, còn ô trống trên tiêu đề nhóm đọc như lỗi hiển thị. Mã thì
    -- để trống thật — không bịa một mã không có trong danh mục.
    buyer.code                                       AS buyer_code,
    COALESCE(buyer.name, 'Chưa khai nhân viên mua')  AS buyer_name,
    project.code                                     AS project_code,
    COALESCE(project.name, 'Không thuộc công trình') AS project_name,
    item.code                                        AS item_code,
    COALESCE(item.name, l.description, 'Không khai vật tư') AS item_name,
    unit.name                                        AS unit_name,
    COALESCE(l.description, v.description)           AS description,
    coa.code                                         AS account_code,
    -- Dấu nhân cho ĐÚNG những cột đến từ thân chứng từ — xem docstring đầu tệp
    -- về chuyện đảo dấu hai lần. Đơn giá không đảo dấu: nó là giá, không phải
    -- lượng.
    l.quantity * nature.factor                       AS quantity,
    l.unit_price_fc                                  AS unit_price_fc,
    l.amount_fc * nature.factor                      AS goods_amount_fc,
    l.landed_cost_fc * nature.factor                 AS landed_cost_fc,
    -- Bốn cột dưới đây lấy từ `gl_postings`: dấu đã nằm trong `debit - credit`.
    goods.amount_fc                                  AS amount_fc,
    goods.amount                                     AS amount,
    COALESCE(vat.amount, 0)                          AS vat_amount,
    -- KHÔNG phải "tổng tiền chứng từ", cũng không phải số phải trả người bán:
    -- `goods.amount` đã gồm chi phí mua phân bổ, còn `vat.amount` chỉ là thuế
    -- của tiền hàng (thuế của khoản chi phí mua gắn `source_line_id = cost.id`).
    -- Tên cột và nhãn layout vì thế nói đúng hai thành phần đang cộng, chứ không
    -- nói "tổng tiền" — người đọc báo cáo không đọc tệp này.
    goods.amount + COALESCE(vat.amount, 0)           AS value_with_vat
FROM purchase_invoice_lines l
JOIN purchase_invoices pi ON pi.id = l.voucher_id
JOIN vouchers v ON v.id = pi.id
CROSS JOIN LATERAL (
    SELECT CASE WHEN pi.kind = 4 THEN -1 ELSE 1 END AS factor
) AS nature
-- Phần ghi vào tài khoản hàng. `SUM` vì một dòng hàng có thể tách thành nhiều
-- dòng phát sinh trên cùng tài khoản (tiền hàng + chi phí mua phân bổ, và chiều
-- phân tích có thể khác nhau) — lấy `LIMIT 1` ở đây là bỏ tiền đi im lặng.
-- `INNER JOIN` chứ không `LEFT`: dòng hàng chưa ghi sổ (hoặc ngoài khoảng ngày,
-- hoặc thuộc sổ khác) KHÔNG có mặt trong báo cáo — đó chính là phép lọc
-- "chỉ chứng từ đã ghi sổ".
JOIN LATERAL (
    SELECT SUM(gp.debit_fc - gp.credit_fc) AS amount_fc,
           SUM(gp.debit - gp.credit)       AS amount,
           MIN(gp.ledger)                  AS ledger,
           MIN(gp.posting_date)            AS posting_date
    FROM gl_postings gp
    WHERE gp.source_line_id = l.id
      AND gp.account_id = l.account_id
      AND gp.ledger = :ledger
      AND gp.posting_date >= :from_date
      AND gp.posting_date <= :to_date
) AS goods ON goods.amount IS NOT NULL
LEFT JOIN LATERAL (
    SELECT SUM(gp.debit - gp.credit) AS amount
    FROM gl_postings gp
    WHERE gp.source_line_id = l.id
      AND l.vat_account_id IS NOT NULL
      AND gp.account_id = l.vat_account_id
      AND gp.ledger = :ledger
      -- Cùng cửa sổ ngày với LATERAL tiền hàng. Hôm nay mọi dòng của một chứng
      -- từ cùng `posting_date` nên điều kiện này không đổi kết quả; thiếu nó là
      -- để hai chân của cùng một phép cộng đứng trên hai phạm vi khác nhau.
      AND gp.posting_date >= :from_date
      AND gp.posting_date <= :to_date
) AS vat ON TRUE
JOIN chart_of_accounts coa ON coa.id = l.account_id
JOIN partners vendor ON vendor.id = pi.vendor_id
LEFT JOIN employees buyer ON buyer.id = pi.buyer_id
LEFT JOIN projects project ON project.id = l.project_id
LEFT JOIN items item ON item.id = l.item_id
LEFT JOIN units_of_measure unit ON unit.id = l.unit_id
WHERE (CAST(:vendor_id AS INTEGER) IS NULL OR pi.vendor_id = :vendor_id)
  AND (CAST(:item_id AS INTEGER) IS NULL OR l.item_id = :item_id)
  AND (CAST(:kind AS INTEGER) IS NULL OR pi.kind = :kind)
  AND (CAST(:buyer_id AS INTEGER) IS NULL OR pi.buyer_id = :buyer_id)
  AND (CAST(:project_id AS INTEGER) IS NULL OR l.project_id = :project_id)
