-- BR-OPB-02: chi tiết đầu kỳ khớp tổng hợp — tổng CÓ DẤU của dòng con
-- (`opening_balance_invoices`) so với dư RÒNG của dòng cha.
--
-- ⚠️ **TỆP NÀY KHÔNG NẰM TRONG `CHECKS`** (xem `registry.py`) — gỡ khỏi
-- registry ở lát **7C-5** (quyết định user 2026-09-06), không phải quên. Nó
-- đứng trong registry từ 4C và **đỏ trên dữ liệu ĐÚNG** suốt từ đó; lát 7C-5
-- đi tìm điều kiện cuối của `arap_matches_control` thì phát hiện ra. Luật áp
-- cho mọi check của thư mục này là một luật: chỉ đăng ký khi câu xanh trên dữ
-- liệu đúng — một check kêu sai dạy người dùng bỏ qua mọi check còn lại.
--
-- **Vì sao nó đỏ oan, và vì sao không công thức nào cứu được ở dạng hiện tại:**
--
--  1. **Cộng hai chiều ở vế dòng cha.** Bản 4C so `SUM(amount)` với
--     `debit + credit`. BR-OPB-03 cho phép một đối tác vừa còn nợ vừa có khoản
--     ứng trước trên CÙNG dòng cha (dư `Nợ 1.000.000 / Có 300.000` với hai
--     chứng từ tổng 1.000.000): vế phải thành 1.300.000 trong khi vế trái có
--     1.000.000. Ca này **đã đóng được** ở 7C-5 — khoản ứng trước từ nay có
--     dòng con của chính nó (`is_advance`), nên hai vế đo cùng một thứ.
--
--  2. **Từ năm thứ hai, dòng cha không còn là "tổng của chi tiết".** Lượt
--     chuyển năm dựng dòng cha N+1 từ `opening_balances(N) + gl_postings(N)`,
--     nên nó gộp cả khoản nợ phát sinh TRONG năm — thứ sống ở `ar_ap_ledger`
--     chứ không ở bảng con này (đường ghi `opening_invoice_id` đã bị bỏ ở
--     7C-3: một sổ phụ hai cửa ghi làm `arap_matches_control` mất ý nghĩa).
--     Hai hình dạng hợp lệ đỏ ngay:
--
--     * hóa đơn đầu kỳ 1.000.000 + một phiếu thu ứng trước 300.000 trong năm
--       ⇒ dòng cha N+1 dư 700.000, chi tiết chuyển sang vẫn 1.000.000;
--     * khách chỉ có khoản ứng trước phát sinh trong năm ⇒ dòng cha N+1 dư Có
--       500.000 và **không dòng con nào**.
--
--     Chi tiết đầu kỳ vì thế là một **tập con** của công nợ đối tác, và không
--     đẳng thức lẫn bao hàm nào giữa tập con ấy với dư ròng là bất biến. Bất
--     biến chỉ sống ở **năm NHẬP**, nơi dòng cha là con số người dùng khai chứ
--     không phải số kết chuyển — mà schema hiện chưa phân biệt được hai loại
--     dòng cha ấy.
--
-- **Điều kiện đăng ký lại:** phân biệt được dòng cha do NHẬP với dòng cha do
-- CHUYỂN NĂM (một cột provenance, hoặc một cách đo khác cho phần kết chuyển),
-- rồi chỉ đo loại thứ nhất. Đó là địa hạt phase 10a cùng với khóa sổ năm.
--
-- **Cái mất khi gỡ, và ai gánh:** đẳng thức này là thứ duy nhất bắt "dòng con
-- bị xóa/sửa thẳng bằng SQL". `arap_matches_control` (đăng ký ở 7C-5) bắt được
-- đúng ca ấy ở phạm vi của nó — xóa một dòng con làm vế sổ phụ tụt trong khi
-- vế sổ cái đứng yên — nhưng phạm vi ấy KHÔNG phủ ba chỗ: dòng cha sổ quản trị
-- (`ledger = 1`), nhóm tạm ứng nhân viên (`detail_kind = 4`), và TK công nợ
-- chưa khai `detail_tracking`. Ba chỗ đó hiện không check nào canh.
--
-- Câu dưới đây giữ nguyên bản đã sửa ở 7C-5 (có dấu, so với dư ròng, bao hàm
-- một chiều) để lần đăng ký lại có chỗ bắt đầu: nó đúng cho dòng cha do NHẬP.
-- Chiều còn-nợ theo nhóm — nhóm phải trả (3) dư Có, nhóm phải thu (2) và tạm
-- ứng nhân viên (4) dư Nợ — và dòng ứng trước đảo dấu của chính nhóm mình.
SELECT ob.id             AS opening_balance_id,
       ob.fiscal_year_id AS fiscal_year_id,
       ob.ledger         AS ledger,
       ob.branch_id      AS branch_id,
       ob.account_id     AS account_id,
       ob.detail_kind    AS detail_kind,
       CASE ob.detail_kind WHEN 3 THEN ob.credit - ob.debit ELSE ob.debit - ob.credit END
           AS parent_amount,
       COALESCE(SUM(i.amount * CASE WHEN i.is_advance THEN -1 ELSE 1 END), 0) AS invoice_total
FROM opening_balances ob
LEFT JOIN opening_balance_invoices i ON i.opening_balance_id = ob.id
WHERE ob.branch_id = :branch_id
  AND ob.detail_kind IN (2, 3, 4)
GROUP BY ob.id, ob.fiscal_year_id, ob.ledger, ob.branch_id, ob.account_id, ob.detail_kind
HAVING COALESCE(SUM(i.amount * CASE WHEN i.is_advance THEN -1 ELSE 1 END), 0)
       > CASE ob.detail_kind WHEN 3 THEN ob.credit - ob.debit ELSE ob.debit - ob.credit END
