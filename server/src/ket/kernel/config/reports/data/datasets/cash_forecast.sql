-- Dataset `cash_forecast`: Dự báo dòng tiền (FR-QUY-032, `docs/srs/03` §5.3) —
-- dự kiến thu (công nợ phải thu còn nợ) và dự kiến chi (công nợ phải trả còn
-- nợ) theo hạn thanh toán. Không có màn hình riêng: đúng chủ trương bước 11
-- phase-06, dự báo là MỘT dataset báo cáo trên hạ tầng metadata phase 5.
--
-- Hai nguồn: chi tiết chứng từ số dư ban đầu (`opening_balance_invoices`,
-- slice 4C) và sổ phụ công nợ do chứng từ mua/bán sinh (`ar_ap_ledger`, lát
-- 7A). Nhánh thứ hai thêm vào ĐÚNG như tệp này dự liệu từ 6E — mở rộng SQL của
-- gói, không sửa một dòng code engine nào. Bỏ sót nó thì từ lát 7B trở đi dự
-- báo im lặng phớt lờ mọi hóa đơn thật, và vẫn ra số.
--
-- `remaining` (VND) tính theo tỷ giá GHI NHẬN nợ của dòng cha: dự báo là câu
-- hỏi "sổ đang treo bao nhiêu", không phải "quy đổi hôm nay được bao nhiêu";
-- phần chênh khi thu/trả thật đi vào 515/635 (FR-SYS-066).
--
-- `bucket` chia mốc theo :to_date (ngày lập dự báo): qua hạn / 0–30 / 31–60 /
-- 61–90 / trên 90 ngày / không ghi hạn — nhóm trình bày của layout 6E.
--
--
-- **Giới hạn đã biết — khoản treo lên NHÂN VIÊN không có tên.** `partner_kind`
-- = 2 trỏ `employees`, không trỏ `partners`, nên phép lấy tên bên dưới bỏ qua
-- chúng: dòng ra `partner_code`/`partner_name` rỗng và gộp thành một nhóm
-- không tên trên layout. Không join bừa còn hơn in tên của một đối tác trùng
-- id (sai mà trông đúng). Mở rộng bằng một nhánh `employees` khi phân hệ tạm
-- ứng có mặt — 7G/phase 9, cùng lúc với công nợ nhân viên.
-- Tham số: :from_date, :to_date, :ledger, :branch_ids (:from_date không dùng
-- nhưng thuộc bộ chuẩn engine luôn truyền).
WITH fy AS (
    SELECT id
    FROM fiscal_years
    WHERE :to_date >= start_date AND :to_date <= end_date
),
open_items AS (
    SELECT b.detail_kind,
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
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR b.branch_id = ANY(:branch_ids))
      AND i.amount_fc > i.paid_amount_fc

    UNION ALL

    -- `target_kind` 0 hóa đơn bán → phải thu (nhóm 2), 1 hóa đơn mua → phải
    -- trả (nhóm 3); ánh xạ về `detail_kind` để phần trình bày bên dưới không
    -- phải biết có hai nguồn.
    SELECT CASE l.target_kind WHEN 0 THEN 2 ELSE 3 END AS detail_kind,
           l.partner_kind,
           l.partner_id,
           l.currency_code,
           l.document_no   AS invoice_no,
           l.document_date AS invoice_date,
           l.due_date,
           l.amount_fc - l.settled_fc AS remaining_fc,
           l.amount - l.settled       AS remaining
    FROM ar_ap_ledger l
    WHERE l.target_kind IN (0, 1)
      AND l.ledger = :ledger
      AND l.is_closed = FALSE
      -- Nhánh 4C bị `JOIN fy` ghim vào năm chứa :to_date; sổ phụ không thuộc
      -- năm nào nên phải tự chặn mốc. Thiếu dòng này thì dự báo lập cho
      -- 31/12 vẫn kéo cả hóa đơn lập tháng 3 năm sau vào — trộn hai kỳ và
      -- vẫn ra số.
      AND l.document_date <= :to_date
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR l.branch_id = ANY(:branch_ids))
)
SELECT CASE oi.detail_kind WHEN 2 THEN 'thu' ELSE 'chi' END AS direction,
       p.code  AS partner_code,
       p.name  AS partner_name,
       oi.invoice_no,
       oi.invoice_date,
       oi.due_date,
       CASE
           WHEN oi.due_date IS NULL                    THEN 'khong-han'
           WHEN oi.due_date < :to_date                 THEN 'qua-han'
           WHEN oi.due_date <= :to_date + 30           THEN '0-30'
           WHEN oi.due_date <= :to_date + 60           THEN '31-60'
           WHEN oi.due_date <= :to_date + 90           THEN '61-90'
           ELSE 'tren-90'
       END AS bucket,
       oi.currency_code,
       oi.remaining_fc,
       oi.remaining
FROM open_items oi
-- `partner_kind = 2` (nhân viên) trỏ `employees`, KHÔNG trỏ `partners`:
-- join thẳng sẽ in tên của một đối tác trùng id — sai mà trông đúng.
LEFT JOIN partners p ON p.id = oi.partner_id AND oi.partner_kind IN (0, 1)
ORDER BY direction, oi.due_date NULLS LAST, p.code, oi.invoice_no
