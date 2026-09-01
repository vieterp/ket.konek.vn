-- BR-GLE-05 ở phạm vi sổ phụ công nợ: tổng CÒN-NỢ của hai sổ phụ (số dư đầu
-- kỳ `opening_balance_invoices` + `ar_ap_ledger`) phải bằng số dư sổ cái của
-- các TK công nợ, đo theo từng (sổ, TK, đối tượng).
--
-- ⚠️ **TỆP NÀY CHƯA NẰM TRONG `CHECKS`** (xem `registry.py`) — cố ý, không
-- phải quên. Nó là bản ghi của thiết kế và của bốn điều kiện phải đóng trước
-- khi đăng ký; đăng ký sớm thì check đỏ trên dữ liệu ĐÚNG, và một check kêu
-- sai là một check người ta học cách bỏ qua — tệ hơn không có check. Bốn điều
-- kiện, cả bốn đều bật ra trên dữ liệu hôm nay (lát 7A: `ar_ap_ledger` còn
-- rỗng, chỉ có số dư đầu kỳ + phiếu thu/chi):
--
--  1. **Bút toán tổng hợp gõ thẳng vào 131/331.** `JournalVoucherService`
--     (FR-GLE-001) ghi `gl_postings` mang chiều đối tác vào TK công nợ mà
--     KHÔNG ghi sổ phụ nào — không có đường nào để nó ghi: `ArApSubledger`
--     chỉ có chủ là chứng từ mua/bán. Bù trừ 131 ↔ 331, xóa nợ, phân loại
--     lại đầu năm đều đi đường này và đều hợp lệ. Vế sổ cái nhích, vế sổ phụ
--     đứng yên, check đỏ. Đây là false positive KHÔNG né được bằng cách viết
--     lại câu SQL — nó là một lỗ hổng của mô hình, không của phép đo.
--  2. **Bên trái tính của TK lưỡng tính không có chi tiết.** 131/331 là
--     `BalanceNature.DUAL`; dòng số dư đầu kỳ ở bên NGƯỢC tính chất (khách
--     ứng trước tiền, trả trước cho người bán) hợp lệ nhưng không treo dòng
--     `opening_balance_invoices` nào — `service._stage_rows` chỉ gắn hóa đơn
--     cho `row.is_natural_side`. Sổ cái có số ấy, sổ phụ không.
--  3. **Lượt chuyển năm được phép làm rơi hóa đơn.** `carry_forward_job.
--     _carry_invoices` trả `invoices_dropped` (hóa đơn không tìm được dòng
--     cha năm mới) và `invoice_overrun_parents` — hai con số ấy tồn tại vì
--     lệch giữa dòng cha (dựng từ dư sổ cái) và tổng hóa đơn con là chuyện
--     ĐÃ BIẾT và đã được báo cho người dùng ngay trên kết quả job.
--  4. **Ranh giới năm chưa có mặt phẳng chung.** Số dư đầu kỳ được chuyển
--     năm (dòng cha + hóa đơn con mới, `paid` về 0), còn `ar_ap_ledger`
--     KHÔNG — một hóa đơn bán của năm N còn nợ sang năm N+1 nằm ở cả hai chỗ:
--     vế sổ cái năm N+1 nhận nó qua dòng số dư đầu kỳ, vế sổ phụ vẫn cầm dòng
--     `ar_ap_ledger` mang `document_date` của năm N. Cột `opening_invoice_id`
--     được khai sẵn cho đúng lượt gộp ấy (xem docstring `ArApLedgerEntry`)
--     nhưng đường ghi nó chưa tồn tại. Chừng nào chưa có, không có cách chọn
--     phạm vi năm nào cho ra một đẳng thức đúng ở cả hai năm.
--
-- Câu dưới đây vì thế giả định **một năm tài chính đang chạy** (năm phủ
-- `CURRENT_DATE`) và bốn điều trên đã đóng. Đẳng thức nó đo, theo từng
-- (sổ, TK, đối tượng):
--
--     dư_sổ_cái = Σ opening_balances(năm).(debit − credit)
--               + Σ gl_postings(trong năm).(debit − credit)
--     dư_sổ_phụ = ± Σ opening_balance_invoices.(amount − paid_amount)
--               ± Σ ar_ap_ledger.(amount − settled)
--
-- Dấu theo LOẠI ĐỐI TÁC chứ không theo số hiệu TK: phải thu (khách hàng) dư
-- Nợ nên vào dương, phải trả (nhà cung cấp) dư Có nên vào âm. Hai vế cùng quy
-- về một trục có dấu thì so được bằng một phép trừ, không cần biết TK nào
-- "tính chất gì".
--
-- **TK công nợ là TK nào**: TK có `detail_tracking` chứa `customer`/`vendor`
-- (cùng cơ chế `detail_matches_control.sql`), KHÔNG phải literal '131'/'331'.
-- Số hiệu TK thuộc gói cấu hình — gói TT133 và gói tự dựng của khách đặt công
-- nợ ở số hiệu khác là chuyện bình thường, và một câu SQL viết cứng số hiệu
-- sẽ lặng lẽ đo 0 dòng ở đó (SRS 19 §9 #1). `employee` đứng ngoài: tạm ứng
-- (141/334) chưa có đường đối trừ theo từng lần trong v1 (xem docstring
-- `opening_balances/settlement_source.py`).
--
-- FULL JOIN chứ không INNER: một đối tượng chỉ có ở vế sổ cái (bút toán quên
-- sổ phụ) và một đối tượng chỉ có ở vế sổ phụ (dòng sổ phụ mồ côi sau khi
-- chứng từ bị xóa bằng SQL) đều là lệch — INNER JOIN mù cả hai chiều.
WITH current_year AS (
    SELECT y.id, y.start_date, y.end_date
    FROM fiscal_years y
    WHERE CURRENT_DATE BETWEEN y.start_date AND y.end_date
),
-- Một dòng cho mỗi (TK công nợ, loại đối tác mà TK ấy theo dõi). `unnest` +
-- lọc hai token: cùng cách đọc `detail_tracking` với `detail_matches_control`,
-- nên gói nào bật theo dõi đối tượng ở đâu thì check đo ở đó.
control AS (
    SELECT a.id AS account_id,
           CASE t.tracking WHEN 'customer' THEN 0 ELSE 1 END AS partner_kind,
           a.code AS account_code
    FROM chart_of_accounts a
    CROSS JOIN LATERAL unnest(a.detail_tracking) AS t(tracking)
    WHERE a.detail_tracking IS NOT NULL
      AND t.tracking IN ('customer', 'vendor')
),
ledger_side AS (
    SELECT ob.ledger, ob.account_id, c.partner_kind, ob.partner_id,
           SUM(ob.debit - ob.credit) AS net
    FROM opening_balances ob
    JOIN current_year y ON y.id = ob.fiscal_year_id
    JOIN control c ON c.account_id = ob.account_id AND c.partner_kind = ob.partner_kind
    WHERE ob.branch_id = :branch_id
    GROUP BY ob.ledger, ob.account_id, c.partner_kind, ob.partner_id
    UNION ALL
    SELECT p.ledger, p.account_id, c.partner_kind, p.partner_id,
           SUM(p.debit - p.credit)
    FROM gl_postings p
    CROSS JOIN current_year y
    JOIN control c ON c.account_id = p.account_id AND c.partner_kind = p.partner_kind
    WHERE p.branch_id = :branch_id
      AND p.partner_id IS NOT NULL
      AND p.posting_date BETWEEN y.start_date AND y.end_date
    GROUP BY p.ledger, p.account_id, c.partner_kind, p.partner_id
),
-- `partner_kind = 0` (khách hàng) vào dương, `1` (nhà cung cấp) vào âm — cùng
-- trục dấu với vế sổ cái ở trên.
subledger_side AS (
    SELECT ob.ledger, ob.account_id, c.partner_kind, ob.partner_id,
           SUM((i.amount - i.paid_amount) * CASE c.partner_kind WHEN 0 THEN 1 ELSE -1 END) AS net
    FROM opening_balance_invoices i
    JOIN opening_balances ob ON ob.id = i.opening_balance_id
    JOIN current_year y ON y.id = ob.fiscal_year_id
    JOIN control c ON c.account_id = ob.account_id AND c.partner_kind = ob.partner_kind
    WHERE i.branch_id = :branch_id
    GROUP BY ob.ledger, ob.account_id, c.partner_kind, ob.partner_id
    UNION ALL
    SELECT l.ledger, l.account_id, c.partner_kind, l.partner_id,
           SUM((l.amount - l.settled) * CASE c.partner_kind WHEN 0 THEN 1 ELSE -1 END)
    FROM ar_ap_ledger l
    JOIN control c ON c.account_id = l.account_id AND c.partner_kind = l.partner_kind
    WHERE l.branch_id = :branch_id
    GROUP BY l.ledger, l.account_id, c.partner_kind, l.partner_id
),
gl AS (
    SELECT ledger, account_id, partner_kind, partner_id, SUM(net) AS net
    FROM ledger_side
    GROUP BY ledger, account_id, partner_kind, partner_id
),
sub AS (
    SELECT ledger, account_id, partner_kind, partner_id, SUM(net) AS net
    FROM subledger_side
    GROUP BY ledger, account_id, partner_kind, partner_id
)
SELECT COALESCE(g.ledger, s.ledger)             AS ledger,
       COALESCE(g.account_id, s.account_id)     AS account_id,
       COALESCE(g.partner_kind, s.partner_kind) AS partner_kind,
       COALESCE(g.partner_id, s.partner_id)     AS partner_id,
       COALESCE(g.net, 0)                       AS ledger_net,
       COALESCE(s.net, 0)                       AS subledger_net,
       COALESCE(g.net, 0) - COALESCE(s.net, 0)  AS difference
FROM gl g
FULL JOIN sub s
  ON  s.ledger = g.ledger AND s.account_id = g.account_id
  AND s.partner_kind = g.partner_kind AND s.partner_id = g.partner_id
WHERE COALESCE(g.net, 0) <> COALESCE(s.net, 0)
