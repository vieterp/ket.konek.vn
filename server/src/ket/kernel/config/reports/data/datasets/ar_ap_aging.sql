-- Dataset `ar_ap_aging`: Tuổi nợ phải thu / phải trả (`docs/srs/05` §5,
-- `docs/srs/06` §5) — lát 7A. Một SQL phục vụ CẢ HAI chiều; hai định nghĩa báo
-- cáo ghim `:direction` bằng `fixed_params` ('thu' / 'chi'), đúng khuôn
-- S03a1/S03a2 của lát 6E-1. Chiều là tham số chứ không phải hai tệp SQL: hai
-- bản chép của cùng phép chia mốc tuổi nợ sẽ lệch nhau ở lần sửa mốc đầu tiên.
--
-- **Hai nguồn, một mặt phẳng.** Công nợ của doanh nghiệp không nằm gọn ở một
-- bảng: khoản nợ mang sang từ trước khi hệ chạy ở `opening_balance_invoices`
-- (4C), khoản do chính hệ sinh ra ở `ar_ap_ledger` (7A). Một báo cáo tuổi nợ
-- chỉ đọc một trong hai là một báo cáo SAI — và sai theo hướng khó thấy nhất,
-- vì nó vẫn ra số. UNION ALL ở đây là chỗ duy nhất hai nguồn gặp nhau.
--
-- `remaining` (VND) là phần giá trị sổ còn treo, tính theo tỷ giá GHI NHẬN nợ
-- — không quy đổi lại theo tỷ giá hôm nay: tuổi nợ trả lời "sổ đang treo bao
-- nhiêu", còn phần chênh khi thu/trả thật đi vào 515/635 (FR-SYS-066).
--
-- Mốc tuổi nợ đếm từ **hạn thanh toán**; hóa đơn không ghi hạn rơi vào nhóm
-- riêng thay vì bị coi là quá hạn 0 ngày — "không ghi hạn" và "đến hạn hôm
-- nay" là hai tình trạng khác nhau, và gộp chúng làm cột "quá hạn" nói dối.
--
--
-- **Giới hạn đã biết — khoản treo lên NHÂN VIÊN không có tên.** `partner_kind`
-- = 2 trỏ `employees`, không trỏ `partners`, nên phép lấy tên bên dưới bỏ qua
-- chúng: dòng ra `partner_code`/`partner_name` rỗng và gộp thành một nhóm
-- không tên trên layout. Không join bừa còn hơn in tên của một đối tác trùng
-- id (sai mà trông đúng). Mở rộng bằng một nhánh `employees` khi phân hệ tạm
-- ứng có mặt — 7G/phase 9, cùng lúc với công nợ nhân viên.
-- Tham số: :from_date, :to_date (mốc tính tuổi nợ), :ledger, :branch_ids,
-- :direction (:from_date không dùng nhưng thuộc bộ chuẩn engine luôn truyền).
WITH fy AS (
    SELECT id
    FROM fiscal_years
    WHERE :to_date >= start_date AND :to_date <= end_date
),
open_items AS (
    -- Nguồn 1: sổ phụ công nợ do chứng từ mua/bán sinh (phase 7).
    SELECT CASE l.target_kind WHEN 0 THEN 'thu' ELSE 'chi' END AS direction,
           l.partner_kind,
           l.partner_id,
           l.currency_code,
           l.document_no  AS invoice_no,
           l.document_date AS invoice_date,
           l.due_date,
           l.amount_fc - l.settled_fc AS remaining_fc,
           l.amount - l.settled       AS remaining
    FROM ar_ap_ledger l
    WHERE l.target_kind IN (0, 1)
      AND l.ledger = :ledger
      AND l.is_closed = FALSE
      AND l.document_date <= :to_date
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR l.branch_id = ANY(:branch_ids))

    UNION ALL

    -- Nguồn 2: chi tiết chứng từ số dư ban đầu (4C) — khoản nợ mang sang.
    SELECT CASE b.detail_kind WHEN 2 THEN 'thu' ELSE 'chi' END AS direction,
           -- 4C cho phép bỏ trống `partner_kind`; nguồn của nó suy từ nhóm
           -- (`_DETAIL_KIND_TO_PARTNER`). Suy đúng như thế ở đây, nếu không
           -- dòng NULL sẽ trượt điều kiện join và mất tên đối tác.
           COALESCE(b.partner_kind, CASE b.detail_kind WHEN 2 THEN 0 ELSE 1 END) AS partner_kind,
           b.partner_id,
           b.currency_code,
           i.invoice_no,
           i.invoice_date,
           i.due_date,
           i.amount_fc - i.paid_amount_fc AS remaining_fc,
           i.amount - i.paid_amount       AS remaining
    FROM opening_balance_invoices i
    JOIN opening_balances b ON b.id = i.opening_balance_id
    JOIN fy ON b.fiscal_year_id = fy.id
    WHERE b.detail_kind IN (2, 3)
      AND b.ledger = :ledger
      AND i.amount_fc > i.paid_amount_fc
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR b.branch_id = ANY(:branch_ids))
)
SELECT oi.direction,
       p.code AS partner_code,
       p.name AS partner_name,
       oi.invoice_no,
       oi.invoice_date,
       oi.due_date,
       CASE
           WHEN oi.due_date IS NULL          THEN NULL
           WHEN oi.due_date >= :to_date      THEN 0
           ELSE (:to_date - oi.due_date)
       END AS days_overdue,
       CASE
           WHEN oi.due_date IS NULL              THEN 'khong-han'
           WHEN oi.due_date >= :to_date          THEN 'chua-den-han'
           WHEN oi.due_date >= :to_date - 30     THEN '1-30'
           WHEN oi.due_date >= :to_date - 60     THEN '31-60'
           WHEN oi.due_date >= :to_date - 90     THEN '61-90'
           ELSE 'tren-90'
       END AS bucket,
       oi.currency_code,
       oi.remaining_fc,
       oi.remaining
FROM open_items oi
-- `partner_kind = 2` (nhân viên) trỏ `employees`, KHÔNG trỏ `partners`:
-- join thẳng sẽ in tên của một đối tác trùng id — sai mà trông đúng.
LEFT JOIN partners p ON p.id = oi.partner_id AND oi.partner_kind IN (0, 1)
WHERE oi.direction = :direction
ORDER BY p.code, oi.due_date NULLS LAST, oi.invoice_no
