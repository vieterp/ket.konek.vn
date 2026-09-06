-- BR-GLE-05 ở phạm vi sổ phụ công nợ: tổng CÒN-NỢ của hai sổ phụ (số dư đầu
-- kỳ `opening_balance_invoices` + `ar_ap_ledger`) phải bằng số dư sổ cái của
-- các TK công nợ, đo theo từng (TK, đối tượng) trên **sổ tài chính**.
--
-- ⚠️ **TỆP NÀY VẪN CHƯA NẰM TRONG `CHECKS`** (xem `registry.py`) — cố ý,
-- không phải quên. Trên dữ liệu ĐÚNG của hôm nay nó vẫn đỏ ở những tình huống
-- hợp lệ, và một check kêu sai là một check người ta học cách bỏ qua.
--
-- Lát **7C-4** đóng thêm hai điều kiện (#2 ở vế chứng từ, #6) và định đăng ký,
-- nhưng review pre-landing tìm ra **ba điều kiện nữa** — một trong số đó có
-- một bài test đang xanh làm bằng chứng. User chốt 2026-09-06: **hoãn đăng ký**,
-- giữ đúng luật đã theo suốt bốn lát (viết tệp trước, đăng ký sau, chỉ đăng ký
-- khi nó xanh trên dữ liệu đúng).
--
-- Điều kiện, và vì sao mỗi cái đóng hay còn mở:
--
--  1. **Bút toán gõ thẳng vào TK công nợ** trước đây ghi `gl_postings` mang
--     chiều đối tác mà KHÔNG ghi sổ phụ nào. Đóng ở 7C-3: `general_ledger.
--     journal` thành nguồn ghi `ar_ap_ledger` thứ ba — dòng ở bên THUẬN sinh
--     khoản mới, dòng ở bên NGƯỢC đi qua `gl_journal_settlements` như một lượt
--     đối trừ. Bù trừ 131 ↔ 331 của cùng đối tác vì thế là hai lượt đối trừ,
--     không phải hai khoản nợ mới.
--
--  2. ✅ **ĐÓNG Ở 7C-4 cho khoản ứng trước sinh từ CHỨNG TỪ.** `settlements`
--     của phiếu thu/chi là TÙY CHỌN và chiều đối tác gắn theo từng dòng, nên
--     một phiếu thu Nợ 111 / Có 131 mang đối tác mà không chọn đối trừ là
--     khoản ứng trước hợp lệ, làm nhích vế sổ cái mà không nhích vế sổ phụ.
--     Luật chung ở `posting/debt_lines.py` đóng ca ấy cho cả ba phân hệ sinh
--     chuyển động công nợ (phiếu thu/chi, chứng từ ngân hàng, chứng từ nghiệp
--     vụ khác): bên ngược không trỏ đích sinh một dòng sổ phụ **chiều ngược**
--     (`ADVANCE_FROM_CUSTOMER`/`ADVANCE_TO_VENDOR`). Bù khoản ứng trước với
--     hóa đơn phát sinh sau đi bằng một chứng từ nghiệp vụ khác: dòng bên
--     thuận trỏ vào khoản ứng trước, dòng bên ngược trỏ vào hóa đơn.
--
--     Khoản ứng trước có ở **số dư đầu kỳ** thì chưa — xem #7.
--
--  3. ⚠️ **CÒN MỞ, hẹp hơn mô tả 7C-3.** Lập luận 4C dựa vào "`paid_amount`
--     chưa sống tới phase 7" đã hết đúng từ **6B**, nên phần lớn điều kiện này
--     là mô tả lỗi thời. Phần `dropped` còn THẬT: `carry_forward_job.
--     _carry_invoices` bỏ hóa đơn khi dư RÒNG của đối tác về 0 trong lúc hóa
--     đơn còn treo — tức đúng ca có khoản ứng trước bù vào. 7C-3 khép nó vào
--     #2 và coi như đóng theo; nhưng #2 chỉ đóng vế CHỨNG TỪ, còn ca này sống
--     ở vế SỐ DƯ ĐẦU KỲ, nên nó đi cùng #7 chứ không đóng cùng #2.
--
--  4. **Nợ mang sang năm sau.** Đóng ở 7C-3 bằng lập luận: dòng cha năm N+1
--     dựng theo TỪNG (TK, tiền tệ, đối tác) từ `opening_balances(N) +
--     gl_postings(N)`, nên khoản nợ vào vế SỔ CÁI năm N+1 qua dòng cha; còn vế
--     SỔ PHỤ thì câu dưới đây **không lọc năm** trên `ar_ap_ledger`, nên chính
--     dòng cũ vẫn được cộng. Hai vế khớp ở mọi năm mà không phải chuyển gì.
--     Cột `opening_invoice_id` vì thế **giữ nguyên trạng thái chưa có đường
--     ghi** — một sổ phụ hai cửa ghi là thứ làm chính check này mất ý nghĩa.
--
--  6. **Đẳng thức không bao giờ đúng trên sổ quản trị.** Engine nhân đôi bút
--     toán sang cả hai sổ (LD-07, `management_lines = None`), còn sổ phụ công
--     nợ **chỉ ghi sổ tài chính** — mọi nguồn đều khóa `ledger = 0`, có chủ
--     đích (nguồn đối trừ của `receivables` cũng chỉ cộng vào sổ ấy). Mỗi
--     khoản công nợ vì thế để lại đúng một dòng lệch ở `ledger = 1` bằng chính
--     số dư của nó. Đóng ở **7C-4** bằng đường thứ nhất trong hai đường mà
--     7C-3 nêu: **lọc `ledger = 0` ngay trong câu** (quyết định user
--     2026-09-06). Đường kia — cho sổ phụ ghi cả hai sổ — kéo theo phải định
--     nghĩa lại đối trừ trên sổ quản trị, nơi tiền thật chỉ trả một lần.
--     Phạm vi ấy vì thế là một phần của HỢP ĐỒNG câu này, không phải thiếu
--     sót: công nợ chỉ sống trên sổ tài chính.
--
--  7. ⚠️ **MỞ — khoản ứng trước ở SỐ DƯ ĐẦU KỲ.** Bản 7C-4 đầu tiên khẳng định
--     bảng hóa đơn đầu kỳ không chứa khoản ứng trước nào vì `parsing.py` cấm
--     dòng bên ngược ghi số chứng từ. Khẳng định ấy SAI: `parsing.py` chỉ cấm
--     dòng bên ngược **mang số chứng từ**, còn bản thân khoản ứng trước vẫn
--     vào cột `credit` của `opening_balances` — tức vế SỔ CÁI — mà không để
--     lại dòng `opening_balance_invoices` nào ở vế sổ phụ. BR-OPB-03 nói thẳng
--     rằng TK lưỡng tính được phép như vậy.
--
--     Bằng chứng nằm sẵn trong một bài test đang XANH:
--     `test_opening_balances_import.py::
--     test_dual_nature_partner_keeps_both_sides_on_one_parent` dựng dòng cha
--     `debit = 1.000.000` / `credit = 300.000` với đúng hai dòng hóa đơn tổng
--     1.000.000 ⇒ câu này trả `difference = -300.000` trên dữ liệu hợp lệ.
--
--     Đóng nó nghĩa là khoản ứng trước đầu kỳ cũng phải có mặt ở vế sổ phụ —
--     một đường ghi từ `opening_balances` vào `ar_ap_ledger` (kèm backfill cho
--     dữ liệu đã nhập), hoặc một cách đo khác cho phần đầu kỳ. Cả hai đều là
--     quyết định của phase 4C/10a, không phải một dòng sửa.
--
--  8. ✅ **ĐÓNG Ở 7C-4.** Câu này neo vào năm tài chính phủ `CURRENT_DATE`,
--     nhưng nhánh `ar_ap_ledger` của vế sổ phụ cố ý KHÔNG lọc năm (#4). Hai
--     điều đó cộng lại cho một ca hỏng im lặng: ngày chạy job không rơi vào
--     năm tài chính nào — đầu tháng 1 trước khi mở niên độ mới — thì vế sổ cái
--     RỖNG còn vế sổ phụ ĐẦY, và `FULL JOIN` biến **mọi** khoản công nợ đang
--     treo thành một dòng đỏ. Sửa bằng một `CROSS JOIN current_year` không
--     kèm vị từ ngày ở nhánh ấy: không lọc năm, nhưng tắt cùng lúc với vế kia.
--
--  9. ⚠️ **MỞ — đối trừ mức CHỨNG TỪ với đối tác/TK mức DÒNG.** Phiếu thu/chi
--     và chứng từ tiền gửi giữ khối đối trừ ở mức chứng từ: `price_settlements`
--     nhận `partner_id` của HEADER và (khác mua/bán/GLE) **không** truyền
--     `account_id`. Còn `posting_mapper` ghi `gl_postings` theo đối tác của
--     TỪNG DÒNG. Không validator nào buộc hai thứ bằng nhau, và BR-QUY-03 so
--     tổng đối trừ với tổng MỌI dòng chứ không riêng dòng công nợ.
--
--     Hệ quả: một phiếu thu 150 gồm Có 131 khách A 100 + Có 511 50, đối trừ
--     150 vào hóa đơn của A, là chứng từ hợp lệ hôm nay — sổ cái nhích 100,
--     sổ phụ nhích 150. Cùng hình dạng với hai dòng hai đối tác khác nhau, và
--     với một đích treo ở TK khác TK mà dòng ghi giảm.
--
--     `posting/debt_lines.record_pair_voucher_debt` vì thế dùng một phép XẤP
--     XỈ: "chứng từ có khối đối trừ ⇒ mọi chuyển động công nợ của nó đã đi qua
--     `settled`". Xấp xỉ ấy đúng với mọi chứng từ mà form dựng ra, và sai đúng
--     ở những hình dạng trên. Đóng nó là siết cash/bank cho khớp mua/bán/GLE
--     (đối tác + TK của dòng công nợ phải khớp khối đối trừ) — một thay đổi
--     PHÁ VỠ với chứng từ hợp lệ hôm nay, nên là quyết định sản phẩm.
--
-- Câu dưới đây giả định **một năm tài chính đang chạy** (năm phủ
-- `CURRENT_DATE`). Đẳng thức nó đo, theo từng (TK, đối tượng):
--
--     dư_sổ_cái = Σ opening_balances(năm).(debit − credit)
--               + Σ gl_postings(trong năm).(debit − credit)
--     dư_sổ_phụ = ± Σ opening_balance_invoices.(amount − paid_amount)
--               ± Σ ar_ap_ledger.(amount − settled)
--
-- Dấu của vế sổ phụ theo **CHIỀU của khoản**, không theo số hiệu TK: khoản
-- phải thu dư Nợ nên vào dương, khoản phải trả dư Có nên vào âm. Với hóa đơn
-- đầu kỳ chiều suy từ loại đối tác; với `ar_ap_ledger` chiều nằm sẵn trong
-- `target_kind` — và đó chính là chỗ khoản ứng trước phải đi ngược lại loại
-- đối tác của nó: tiền khách ứng trước là một khoản PHẢI TRẢ mang
-- `partner_kind = 0`. Suy dấu theo `partner_kind` ở đây sẽ cộng nó vào dương
-- và làm lệch đúng hai lần số dư của nó.
--
-- **TK công nợ là TK nào**: TK có `detail_tracking` chứa `customer`/`vendor`
-- (cùng cơ chế `detail_matches_control.sql`), KHÔNG phải literal '131'/'331'.
-- Số hiệu TK thuộc gói cấu hình — gói TT133 và gói tự dựng của khách đặt công
-- nợ ở số hiệu khác là chuyện bình thường, và một câu SQL viết cứng số hiệu
-- sẽ lặng lẽ đo 0 dòng ở đó (SRS 19 §9 #1). `employee` đứng ngoài: tạm ứng
-- (141/334) chưa có đường đối trừ theo từng lần trong v1 (xem docstring
-- `opening_balances/settlement_source.py`). Cùng phạm vi ấy được
-- `posting/debt_lines.py` giữ đúng: `_TRACKING_BY_PARTNER_KIND` không có mặt
-- `PartnerKind.EMPLOYEE`.
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
           CASE t.tracking WHEN 'customer' THEN 0 ELSE 1 END AS partner_kind
    FROM chart_of_accounts a
    CROSS JOIN LATERAL unnest(a.detail_tracking) AS t(tracking)
    WHERE a.detail_tracking IS NOT NULL
      AND t.tracking IN ('customer', 'vendor')
),
ledger_side AS (
    SELECT ob.account_id, c.partner_kind, ob.partner_id,
           SUM(ob.debit - ob.credit) AS net
    FROM opening_balances ob
    JOIN current_year y ON y.id = ob.fiscal_year_id
    JOIN control c ON c.account_id = ob.account_id AND c.partner_kind = ob.partner_kind
    WHERE ob.branch_id = :branch_id
      AND ob.ledger = 0
    GROUP BY ob.account_id, c.partner_kind, ob.partner_id
    UNION ALL
    SELECT p.account_id, c.partner_kind, p.partner_id,
           SUM(p.debit - p.credit)
    FROM gl_postings p
    CROSS JOIN current_year y
    JOIN control c ON c.account_id = p.account_id AND c.partner_kind = p.partner_kind
    WHERE p.branch_id = :branch_id
      AND p.ledger = 0
      AND p.partner_id IS NOT NULL
      AND p.posting_date BETWEEN y.start_date AND y.end_date
    GROUP BY p.account_id, c.partner_kind, p.partner_id
),
subledger_side AS (
    -- Hóa đơn đầu kỳ: chiều suy từ loại đối tác, vì bảng CON này chỉ chứa
    -- khoản nợ. Khoản ứng trước đầu kỳ nằm ở cột `credit` của dòng CHA và
    -- không có dòng con nào — đó chính là điều kiện #7 còn mở.
    SELECT ob.account_id, c.partner_kind, ob.partner_id,
           SUM((i.amount - i.paid_amount) * CASE c.partner_kind WHEN 0 THEN 1 ELSE -1 END) AS net
    FROM opening_balance_invoices i
    JOIN opening_balances ob ON ob.id = i.opening_balance_id
    JOIN current_year y ON y.id = ob.fiscal_year_id
    JOIN control c ON c.account_id = ob.account_id AND c.partner_kind = ob.partner_kind
    WHERE i.branch_id = :branch_id
      AND ob.ledger = 0
    GROUP BY ob.account_id, c.partner_kind, ob.partner_id
    UNION ALL
    -- `SettlementTargetKind`: 1 hóa đơn mua, 4 phải trả ghi tay, 5 khách ứng
    -- trước ⇒ chiều PHẢI TRẢ, vào âm. Còn lại (0 hóa đơn bán, 3 phải thu ghi
    -- tay, 6 trả trước người bán) ⇒ phải thu, vào dương.
    SELECT l.account_id, c.partner_kind, l.partner_id,
           SUM((l.amount - l.settled)
               * CASE WHEN l.target_kind IN (1, 4, 5) THEN -1 ELSE 1 END)
    FROM ar_ap_ledger l
    -- `CROSS JOIN` mà KHÔNG có vị từ ngày: nhánh này cố ý không lọc năm (điều
    -- kiện #4), nhưng nó phải TẮT cùng lúc với vế sổ cái. Thiếu phép nối này
    -- thì một ngày chạy job nằm ngoài mọi năm tài chính — đầu tháng 1 trước
    -- khi mở niên độ mới — cho vế sổ cái RỖNG và vế sổ phụ ĐẦY, và `FULL JOIN`
    -- biến mọi khoản công nợ đang treo thành một dòng đỏ (điều kiện #8).
    CROSS JOIN current_year y
    JOIN control c ON c.account_id = l.account_id AND c.partner_kind = l.partner_kind
    WHERE l.branch_id = :branch_id
      AND l.ledger = 0
    GROUP BY l.account_id, c.partner_kind, l.partner_id
),
gl AS (
    SELECT account_id, partner_kind, partner_id, SUM(net) AS net
    FROM ledger_side
    GROUP BY account_id, partner_kind, partner_id
),
sub AS (
    SELECT account_id, partner_kind, partner_id, SUM(net) AS net
    FROM subledger_side
    GROUP BY account_id, partner_kind, partner_id
)
SELECT COALESCE(g.account_id, s.account_id)     AS account_id,
       COALESCE(g.partner_kind, s.partner_kind) AS partner_kind,
       COALESCE(g.partner_id, s.partner_id)     AS partner_id,
       COALESCE(g.net, 0)                       AS ledger_net,
       COALESCE(s.net, 0)                       AS subledger_net,
       COALESCE(g.net, 0) - COALESCE(s.net, 0)  AS difference
FROM gl g
FULL JOIN sub s
  ON  s.account_id = g.account_id
  AND s.partner_kind = g.partner_kind AND s.partner_id = g.partner_id
WHERE COALESCE(g.net, 0) <> COALESCE(s.net, 0)
