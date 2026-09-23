-- Giá trị tồn kho khớp số dư tài khoản kho (BR-STK-03, FR-GLE-032 — lát 8D).
--
-- **CỐ Ý ĐỨNG NGOÀI `CHECKS`** (quyết định user 2026-09-23, sau review thù địch).
-- Cùng lý do và cùng số phận với `opening_detail_matches_control.sql`: nó đỏ
-- trên dữ liệu ĐÚNG. Gốc rễ là một bất đối xứng của phân hệ mua: bút toán TK
-- kho nằm trên **hóa đơn**, còn dòng sổ kho nằm trên **phiếu kho sinh ra từ
-- hóa đơn** — hai chứng từ khác nhau — nên phép loại trừ "bỏ chứng từ còn dòng
-- chưa tính giá" chỉ cắt được MỘT vế. Ba luồng đúng làm nó kêu:
--
--   1. **Trả lại hàng mua.** `PUR,tra-lai-hang-mua` ghi Nợ 331 / Có 156 trên
--      hóa đơn; phiếu XK sinh ra cố ý KHÔNG có giá (`purchase/inventory_lines.py`:
--      giá xuất là việc của engine) nên movement `PENDING` bị loại khỏi vế sổ
--      kho, còn Có 156 của hóa đơn ở lại vế sổ cái.
--   2. **Hóa đơn mua "chưa nhập kho".** Dòng hàng không khai kho → ghi Nợ 156
--      mà KHÔNG sinh phiếu kho nào. Đây là việc còn thiếu **có hỗ trợ** (nhóm
--      `chua-nhap-kho` + hành động `stock-in` của BFF), không phải hỏng dữ liệu.
--   3. **Sắp xếp lại thứ tự trong ngày.** `reorder_day` hạ movement NK sinh từ
--      hóa đơn về `STALE` → bị loại, trong khi Nợ 156 của hóa đơn ở lại.
--
-- **Ba điều kiện để đăng ký lại** (dự kiến 8F, cùng "báo cáo chênh lệch sổ kho"):
--
--   a. loại trừ phải đi theo **chuỗi chứng từ** (`vouchers.source_document_id`),
--      không theo một chứng từ lẻ;
--   b. trạng thái "đã hạch toán TK kho, chưa sinh phiếu kho" phải được mô hình
--      hóa thành **một cột của báo cáo**, không thành một mệnh đề loại trừ —
--      tha nó đi là tha luôn đúng hình dạng FR-STK-042 sinh ra để cảnh báo, và
--      lúc ấy check còn lại gần như không bắt được gì;
--   c. có bài kiểm đỏ-thật cho mỗi loại lệch còn lại SAU khi (a) và (b) đóng.
--
-- Tới lúc đó, chiều giá trị vẫn có người canh: `GET /inventory/costing/uncosted`
-- (FR-STK-008) chỉ ra chứng từ chưa tính giá, và nhóm `chua-nhap-kho`/
-- `chua-xuat-kho` chỉ ra hóa đơn chưa có phiếu kho.
--
-- Phần dưới là câu lệnh như đã viết, giữ nguyên để lát sau sửa tiếp.
--
-- **So DÒNG CHẢY trong từng năm tài chính, không so số dư lũy kế.** Số dư đầu
-- năm sống ở `opening_balances` chứ không ở `gl_postings`, và năm chưa chạy
-- chuyển năm thì chưa có dòng đầu kỳ nào — so số dư sẽ đỏ oan trên đúng những
-- dataset đang làm việc bình thường. Dòng chảy thì không cần số đầu kỳ: tổng
-- phát sinh trên TK kho trong năm phải bằng tổng biến động giá trị sổ kho
-- trong năm, và một movement có giá mà không có bút toán (hoặc ngược lại) lộ
-- ra ngay ở chính năm phát sinh.
--
-- Movement vật chất hóa từ lớp tồn đầu kỳ (8C-1) mang ngày `năm.start − 1`.
-- Đó **không** phải "ngoài mọi năm" như bản đầu của tệp này tưởng: khi niên độ
-- trước tồn tại (GL chạy từ 2025, phân hệ kho bật đầu 2026), ngày ấy rơi vào
-- ĐÚNG ngày cuối của năm trước, và số dư đầu kỳ thì sống ở `opening_balances`
-- chứ không ở `gl_postings` — vế sổ kho có, vế sổ cái không, đỏ vĩnh viễn.
-- Lọc thẳng theo `opening_layer_id`: tồn đầu kỳ là SỐ DƯ, không phải dòng chảy.
-- `carry_forward` nhóm 5 cố ý không sinh movement nên không cần lọc gì thêm.
--
-- **Bỏ qua chứng từ còn dòng chưa tính giá** (`cost_state` ngoài {1 đã tính,
-- 3 không áp dụng}): giá vốn chưa có thì bút toán 632 chưa ghi, hai vế lệch là
-- ĐÚNG và tạm thời. Danh sách ấy đã có cửa riêng — `GET /inventory/costing/
-- uncosted` (FR-STK-008). Một check kêu trên trạng thái tạm thời dạy người
-- dùng bỏ qua mọi check còn lại.
--
-- Dòng hàng giữ hộ (`is_custodial`) không có giá và không có bút toán
-- (BR-STK-07) — bỏ ở cả hai vế, bằng cách bỏ ở vế sổ kho (vế sổ cái vốn đã
-- không có gì).
--
-- TK kho nhận diện qua `default_accounts` của CHÍNH gói chứa tài khoản, không
-- qua tiền tố cứng trong câu lệnh: hệ thống tài khoản là dữ liệu của gói cấu
-- hình (bài học `einvoice_matches_revenue.sql`). Năm purpose là những purpose
-- mà giá trị của chúng nằm trong `inventory_movements`; `goods_in_transit`
-- (151, hàng mua đang đi đường) và `work_in_progress` (154) cố ý đứng ngoài —
-- chúng chưa/không phải hàng trong kho.
--
-- ledger 0 = sổ tài chính. direction 1 nhập / -1 xuất.
WITH stock_accounts AS (
    SELECT DISTINCT a.id
    FROM chart_of_accounts a
    JOIN default_accounts d ON d.package_id = a.package_id
    WHERE d.purpose IN (
              'inventory_goods', 'raw_materials', 'finished_goods',
              'tools_supplies', 'goods_on_consignment'
          )
      AND a.code LIKE d.account_code || '%'
),
uncosted_vouchers AS (
    SELECT DISTINCT m.voucher_id
    FROM inventory_movements m
    WHERE m.branch_id = :branch_id
      AND m.voucher_id IS NOT NULL
      AND m.cost_state NOT IN (1, 3)
),
ledger_flow AS (
    SELECT fy.id AS fiscal_year_id,
           SUM(p.debit - p.credit) AS ledger_net
    FROM gl_postings p
    JOIN vouchers v ON v.id = p.voucher_id
    JOIN fiscal_years fy ON v.posting_date BETWEEN fy.start_date AND fy.end_date
    WHERE p.branch_id = :branch_id
      AND p.ledger = 0
      AND p.account_id IN (SELECT id FROM stock_accounts)
      AND p.voucher_id NOT IN (SELECT voucher_id FROM uncosted_vouchers)
    GROUP BY fy.id
),
stock_flow AS (
    SELECT fy.id AS fiscal_year_id,
           SUM(m.direction * m.amount) AS stock_net
    FROM inventory_movements m
    JOIN fiscal_years fy ON m.posting_date BETWEEN fy.start_date AND fy.end_date
    WHERE m.branch_id = :branch_id
      AND NOT m.is_custodial
      AND m.opening_layer_id IS NULL
      AND m.amount IS NOT NULL
      AND (m.voucher_id IS NULL OR m.voucher_id NOT IN (SELECT voucher_id FROM uncosted_vouchers))
    GROUP BY fy.id
)
SELECT COALESCE(l.fiscal_year_id, s.fiscal_year_id) AS fiscal_year_id,
       COALESCE(l.ledger_net, 0) AS ledger_net,
       COALESCE(s.stock_net, 0)  AS stock_net,
       COALESCE(l.ledger_net, 0) - COALESCE(s.stock_net, 0) AS difference
FROM ledger_flow l
FULL JOIN stock_flow s ON s.fiscal_year_id = l.fiscal_year_id
WHERE COALESCE(l.ledger_net, 0) <> COALESCE(s.stock_net, 0)
