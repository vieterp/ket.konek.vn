-- Dataset `ar_ap_settlements`: từng LƯỢT đối trừ công nợ — ai trả, trả cho
-- khoản nào, ngày nào, và cách bao nhiêu ngày so với ngày hóa đơn / hạn thanh
-- toán. Nền của SRS 06 §5.2 #10 (tổng hợp thanh toán công nợ khách hàng) và #11
-- (báo cáo ngày thanh toán theo khách hàng); `:direction` mở sẵn chiều phải trả
-- cho một lát sau.
--
-- **Đây KHÔNG phải bản chép của `ar_ap_open_items`, và khác biệt là ĐỘ HẠT.**
-- Dataset công nợ trả lời "còn treo bao nhiêu tại ngày X" — một con số TỒN, gộp
-- mọi lượt đối trừ của một khoản thành một cột. Câu này trả lời "trong kỳ đã thu
-- những lượt nào" — một dòng cho mỗi lượt, và nó cắt theo CẢ `:from_date` lẫn
-- `:to_date` vì nó là phát sinh chứ không phải số dư. Hai câu hỏi ấy dùng chung
-- đúng một khối nguồn (`settlement_rows.sql`) và không dùng chung gì thêm.
--
-- **Đích của một lượt đối trừ nằm ở MỘT TRONG HAI bảng**, và cặp
-- `(target_kind, target_id)` là thứ nói bảng nào: `ar_ap_ledger` cho khoản hệ
-- sinh ra (0/1/3/4/5/6), `opening_balance_invoices` cho nợ mang sang và ứng
-- trước đầu kỳ (2/7). Nối cả hai rồi `COALESCE` — bỏ nhánh thứ hai là để lượt
-- thu một khoản nợ mang sang biến mất khỏi báo cáo thanh toán trong khi chính
-- khoản ấy vẫn giảm trên bảng công nợ.
--
-- **`amount - fx_diff`, không phải `amount`.** Cột `amount` của bảng đối trừ là
-- VND theo tỷ giá **THANH TOÁN**; phần VND thật sự giải phóng trên sổ là
-- `amount - fx_diff` (phần chênh đi vào 515/635 — FR-SYS-066), và đó là con số
-- `apply_settlement_rows` cộng vào khoản đích. Cộng `amount` trần ở đây làm tổng
-- của tờ "tổng hợp thanh toán" thôi khớp cột "đã thu" của tờ công nợ, trên đúng
-- những hóa đơn ngoại tệ — hai tờ nói hai số về cùng một lượt thu.
--
-- **Chiều đọc từ ĐÍCH, cùng ánh xạ với `ar_ap_open_items`**: lượt thu một hóa
-- đơn bán là "thu", lượt trả một hóa đơn mua là "chi". Với nợ mang sang thì chiều
-- KHÔNG suy được từ `target_kind` — số dư đầu kỳ có cả hai chiều, và chiều nằm ở
-- `detail_kind` của dòng cha. Khoản ứng trước lại đi NGƯỢC chiều của dòng cha ấy
-- (luật của kernel, xem `SettlementTargetKind.OPENING_ADVANCE`).
--
-- **`days_late` rỗng khi khoản nợ không ghi hạn**, không phải 0: "trả đúng hạn"
-- và "không có hạn để mà trễ" là hai câu khác nhau, và gộp chúng làm cột ấy nói
-- dối theo hướng dễ chịu. Cùng lý do `ar_ap_open_items` tách nhóm `khong-han`.
--
-- *Giới hạn đã biết — không có cột TRUNG BÌNH.* SRS #11 hỏi "ngày thanh toán
-- theo khách hàng"; tờ giấy liệt kê từng lượt thu kèm số ngày, gộp theo khách
-- hàng. Số ngày trung bình thì **không in được ở bản này**: `layout.totals` chỉ
-- biết CỘNG, không có hàm gộp nào khác, và mở `AVG` là nới hợp đồng layout
-- engine — việc của một lát khác, không phải một phép chia lén trong SQL báo cáo.
--
-- *Giới hạn đã biết — khoản treo lên NHÂN VIÊN không có tên*, cùng lời hẹn với
-- `ar_ap_open_items`: `partner_kind = 2` trỏ `employees` chứ không trỏ
-- `partners`, và join thẳng sẽ in tên của một đối tác trùng id.
--
-- Tham số: :from_date, :to_date (khoảng NGÀY GHI SỔ của chứng từ thanh toán),
-- :ledger, :branch_ids, :direction, :partner_id.
WITH rows_of_settlements AS (
    -- #include: settlement_rows.sql
),
targets AS (
    SELECT s.voucher_id,
           s.target_kind,
           -- Chiều tính NGAY Ở ĐÂY, không ở mệnh đề ngoài: số dư đầu kỳ
           -- (`target_kind` 2/7) có cả hai chiều, và chiều của nó nằm ở
           -- `detail_kind` của DÒNG CHA chứ không ở loại đích. Một `CASE` chỉ
           -- đọc `target_kind` sẽ dán nhãn "phải thu" cho mọi khoản nợ mang
           -- sang, kể cả nợ ta còn thiếu nhà cung cấp.
           --
           -- Ánh xạ này phải TRÙNG `ar_ap_open_items`: lệch một giá trị thì tờ
           -- thanh toán và tờ công nợ nói hai điều về cùng một lượt thu, và
           -- không con số nào trên hai tờ chỉ ra chỗ lệch.
           CASE
               WHEN obi.id IS NOT NULL THEN
                   CASE obi.detail_kind
                       WHEN 2 THEN CASE WHEN obi.is_advance THEN 'chi' ELSE 'thu' END
                       ELSE        CASE WHEN obi.is_advance THEN 'thu' ELSE 'chi' END
                   END
               WHEN s.target_kind IN (0, 3, 6) THEN 'thu'
               ELSE 'chi'
           END                                        AS direction,
           s.amount_fc,
           s.amount - s.fx_diff AS amount,
           COALESCE(d.partner_kind, obi.partner_kind) AS partner_kind,
           COALESCE(d.partner_id, obi.partner_id)     AS partner_id,
           COALESCE(d.ledger, obi.ledger)             AS ledger,
           COALESCE(d.document_no, obi.invoice_no)    AS document_no,
           COALESCE(d.document_date, obi.invoice_date) AS document_date,
           COALESCE(d.due_date, obi.due_date)         AS due_date,
           COALESCE(d.currency_code, obi.currency_code) AS currency_code
    FROM rows_of_settlements s
    LEFT JOIN ar_ap_ledger d
           ON d.id = s.target_id
          AND s.target_kind IN (0, 1, 3, 4, 5, 6)
    LEFT JOIN (
        -- Chi tiết chứng từ số dư ban đầu, đã kéo sẵn phần thuộc dòng cha:
        -- `partner_id`/`ledger` nằm ở `opening_balances`, không ở dòng chi tiết.
        -- 4C cho phép bỏ trống `partner_kind`; nguồn của nó suy từ nhóm
        -- (`_DETAIL_KIND_TO_PARTNER`), nên suy đúng như thế ở đây.
        SELECT i.id,
               b.partner_id,
               COALESCE(b.partner_kind, CASE b.detail_kind WHEN 2 THEN 0 ELSE 1 END)
                   AS partner_kind,
               b.ledger,
               b.currency_code,
               i.invoice_no,
               i.invoice_date,
               i.due_date,
               i.is_advance,
               b.detail_kind
        FROM opening_balance_invoices i
        JOIN opening_balances b ON b.id = i.opening_balance_id
    ) AS obi
           ON obi.id = s.target_id
          AND s.target_kind IN (2, 7)
    -- Chỉ lượt đối trừ của chứng từ ĐÃ GHI SỔ, trong kỳ. Dòng đối trừ ghi xuống
    -- bảng lúc **cất** chứng từ (`_write_settlements`), còn phần nhích `settled`
    -- chỉ xảy ra lúc **ghi sổ** (`apply_settlements`) — liệt kê mọi dòng trong
    -- bảng là in ra những lượt thu chưa có đồng nào lên sổ.
    WHERE EXISTS (
        SELECT 1
        FROM vouchers sv
        WHERE sv.id = s.voucher_id
          AND sv.status = 2
          AND sv.posting_date >= :from_date
          AND sv.posting_date <= :to_date
    )
)
SELECT t.direction,
       t.ledger,
       v.branch_id,
       v.posting_date               AS settled_on,
       v.voucher_no                 AS settlement_no,
       v.document_type,
       p.code                       AS partner_code,
       p.name                       AS partner_name,
       t.document_no,
       t.document_date,
       t.due_date,
       -- Bao nhiêu ngày kể từ ngày chứng từ nợ tới ngày tiền lên sổ.
       (v.posting_date - t.document_date) AS days_to_pay,
       -- Trễ bao nhiêu ngày so với HẠN. Rỗng khi khoản nợ không ghi hạn, và âm
       -- khi trả trước hạn — cả hai đều là câu trả lời thật, không phải 0.
       CASE
           WHEN t.due_date IS NULL THEN NULL
           ELSE (v.posting_date - t.due_date)
       END                          AS days_late,
       t.currency_code,
       t.amount_fc,
       t.amount
FROM targets t
JOIN vouchers v ON v.id = t.voucher_id
-- `partner_kind = 2` (nhân viên) trỏ `employees`, KHÔNG trỏ `partners`.
LEFT JOIN partners p ON p.id = t.partner_id AND t.partner_kind IN (0, 1)
WHERE t.ledger = :ledger
  AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR v.branch_id = ANY(:branch_ids))
  AND (
      CAST(:partner_id AS INTEGER) IS NULL
      OR (t.partner_id = :partner_id AND t.partner_kind IN (0, 1))
  )
  AND t.direction = :direction
