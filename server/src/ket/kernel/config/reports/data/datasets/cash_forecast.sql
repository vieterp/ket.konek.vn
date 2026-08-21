-- Dataset `cash_forecast`: Dự báo dòng tiền (FR-QUY-032, `docs/srs/03` §5.3) —
-- dự kiến thu (công nợ phải thu còn nợ) và dự kiến chi (công nợ phải trả còn
-- nợ) theo hạn thanh toán. Không có màn hình riêng: đúng chủ trương bước 11
-- phase-06, dự báo là MỘT dataset báo cáo trên hạ tầng metadata phase 5.
--
-- Nguồn v1 là chi tiết chứng từ số dư ban đầu (`opening_balance_invoices`,
-- slice 4C) — nguồn công nợ duy nhất trước phase 7. Khi module bán/mua ra đời,
-- hóa đơn thật vào dự báo bằng cách MỞ RỘNG chính SQL này (UNION nhánh đọc
-- `ar_ap_ledger`) — sửa dữ liệu gói, không sửa code engine.
--
-- `remaining` (VND) tính theo tỷ giá GHI NHẬN nợ của dòng cha: dự báo là câu
-- hỏi "sổ đang treo bao nhiêu", không phải "quy đổi hôm nay được bao nhiêu";
-- phần chênh khi thu/trả thật đi vào 515/635 (FR-SYS-066).
--
-- `bucket` chia mốc theo :to_date (ngày lập dự báo): qua hạn / 0–30 / 31–60 /
-- 61–90 / trên 90 ngày / không ghi hạn — nhóm trình bày của layout 6E.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids (:from_date không dùng
-- nhưng thuộc bộ chuẩn engine luôn truyền).
WITH fy AS (
    SELECT id
    FROM fiscal_years
    WHERE :to_date >= start_date AND :to_date <= end_date
),
open_items AS (
    SELECT b.detail_kind,
           b.partner_kind,
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
LEFT JOIN partners p ON p.id = oi.partner_id
ORDER BY direction, oi.due_date NULLS LAST, p.code, oi.invoice_no
