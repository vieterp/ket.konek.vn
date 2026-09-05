-- BR-GLE-05 ở phạm vi sổ phụ công nợ: tổng CÒN-NỢ của hai sổ phụ (số dư đầu
-- kỳ `opening_balance_invoices` + `ar_ap_ledger`) phải bằng số dư sổ cái của
-- các TK công nợ, đo theo từng (sổ, TK, đối tượng).
--
-- ⚠️ **TỆP NÀY CHƯA NẰM TRONG `CHECKS`** (xem `registry.py`) — cố ý, không
-- phải quên. Nó là bản ghi của thiết kế và của những điều kiện phải đóng
-- trước khi đăng ký; đăng ký sớm thì check đỏ trên dữ liệu ĐÚNG, và một check
-- kêu sai là một check người ta học cách bỏ qua — tệ hơn không có check.
--
-- Bốn điều kiện đầu ghi ở lát 7A. Lát **7C-3** đóng ba, và trong lúc làm lộ
-- ra điều kiện **thứ năm** — thứ duy nhất còn chặn lượt đăng ký:
--
--  1. ✅ **ĐÓNG Ở 7C-3.** Bút toán tổng hợp gõ thẳng vào 131/331 trước đây ghi
--     `gl_postings` mang chiều đối tác mà KHÔNG ghi sổ phụ nào. Nay
--     `general_ledger.journal` là nguồn ghi `ar_ap_ledger` thứ ba (quyết định
--     user 2026-09-05): dòng ở bên THUẬN tính chất công nợ sinh một khoản mới
--     (`JOURNAL_RECEIVABLE`/`JOURNAL_PAYABLE`), dòng ở bên NGƯỢC đi qua
--     `gl_journal_settlements` như một lượt đối trừ. Bù trừ 131 ↔ 331 của cùng
--     một đối tác vì thế là hai lượt đối trừ, không phải hai khoản nợ mới —
--     phân biệt được hai ca ấy là chính điều kiện này.
--
--  2. ⚠️ **CÒN MỞ — và rộng hơn mô tả cũ.** Bản 7A viết điều kiện này ở phạm
--     vi số dư đầu kỳ: dòng ở bên NGƯỢC tính chất của TK lưỡng tính (khách ứng
--     trước, trả trước người bán) hợp lệ nhưng không treo dòng
--     `opening_balance_invoices` nào — `parsing.py` còn CẤM nó ghi số chứng từ.
--     Lát 7C-3 tìm thấy lỗ ấy không giới hạn ở số dư đầu kỳ: `settlements` của
--     phiếu thu/chi (`cash_book/schemas.py`) là **tùy chọn**, và dimension đối
--     tác gắn theo TỪNG DÒNG (`cash_book/posting_mapper.py`), nên một phiếu
--     thu Nợ 111 / Có 131 mang đối tác mà không chọn đối trừ là một khoản ứng
--     trước hợp lệ, làm nhích vế sổ cái mà không nhích vế sổ phụ. Chứng từ
--     nghiệp vụ khác giữ đúng hành vi ấy ở 7C-3 (bên ngược không trỏ đích thì
--     không sinh gì) — chặn ở một phân hệ mà thả ở phân hệ kia là hai luật cho
--     cùng một hình dạng.
--
--     **Đây là điều kiện thứ năm, và là thứ duy nhất còn chặn.** Đóng nó nghĩa
--     là khoản ứng trước cũng thành một dòng sổ phụ chiều ngược, đối trừ được
--     với hóa đơn phát sinh sau — một cơ chế chưa tồn tại, vì đường đối trừ
--     hiện có đi từ chứng từ TIỀN vào hóa đơn, không phải hóa đơn vào khoản
--     ứng trước. Đã chốt tách thành lát **7C-4** (user, 2026-09-05).
--
--  3. ✅ **ĐÓNG Ở 7C-3, phần lớn là mô tả lỗi thời.** `carry_forward_job.
--     _carry_invoices` trả `invoices_dropped` và `invoice_overrun_parents`;
--     lập luận 4C của hai con số ấy là "`paid_amount` chưa sống tới phase 7
--     nên các khoản trả trong năm trừ vào dư cha mà không trừ vào hóa đơn
--     nào". Điều đó đã hết đúng từ **6B**: `opening_balances/
--     settlement_source.py` cộng cả `paid_amount` lẫn `paid_amount_fc` mỗi
--     lượt đối trừ. Phần `dropped` còn thật chỉ xảy ra khi dư RÒNG của đối
--     tác về 0 trong lúc hóa đơn còn treo — tức là đúng ca có khoản ứng trước
--     bù vào, tức là điều kiện #2.
--
--  4. ✅ **ĐÓNG Ở 7C-3 bằng lập luận, không bằng mã.** Nỗi lo 7A là một hóa
--     đơn bán của năm N còn nợ sang năm N+1 nằm ở cả hai vế và không có phạm
--     vi năm nào cho ra đẳng thức đúng ở cả hai năm. Đọc `sql/
--     carry_forward.sql` cho thấy lo thừa: dòng cha năm N+1 được dựng theo
--     TỪNG (TK, tiền tệ, đối tác) từ `opening_balances(N) + gl_postings(N)`,
--     nên khoản nợ ấy vào vế SỔ CÁI năm N+1 qua dòng cha; còn vế SỔ PHỤ thì
--     câu dưới đây **không lọc năm** trên `ar_ap_ledger`, nên chính dòng cũ
--     vẫn được cộng. Hai vế khớp, ở mọi năm, mà không phải chuyển gì cả.
--
--     Vì thế cột `opening_invoice_id` (khai sẵn ở 7A cho lượt gộp ấy) **giữ
--     nguyên trạng thái chưa có đường ghi** — quyết định user 2026-09-05.
--     Đường ghi ấy chỉ cần khi muốn chi tiết số dư đầu kỳ năm mới liệt kê lại
--     mọi khoản còn nợ (FR-OPB-007 ở dạng mạnh); lúc ấy nó thành đường ghi
--     THỨ HAI vào `ar_ap_ledger`, và một sổ phụ hai cửa ghi là thứ làm chính
--     check này mất ý nghĩa. Để ngỏ cho phase 10a quyết trên dữ liệu nhiều năm.
--
--  6. ⚠️ **CÒN MỞ — tìm thấy ở 7C-3, không phải lỗi của lát nào.** Câu dưới
--     đây đo theo TỪNG SỔ, mà hai vế không sống trên cùng tập sổ: engine nhân
--     đôi bút toán sang cả hai sổ (LD-07, `management_lines = None`), còn sổ
--     phụ công nợ **chỉ ghi sổ tài chính** — `purchase`, `sales` và
--     `general_ledger.journal` đều khóa `ledger = 0`, có chủ đích (nguồn đối
--     trừ của `receivables` cũng chỉ cộng vào sổ ấy, nên dòng sổ quản trị sẽ
--     không bao giờ đóng; xem docstring `purchase._subledger_entries`). Hệ quả:
--     mỗi khoản công nợ để lại đúng một dòng lệch ở `ledger = 1`, bằng chính
--     số dư của nó — với MỌI nguồn, không riêng chứng từ GLE.
--
--     Hai đường đóng, cả hai đều là lựa chọn thật: lọc `ledger = 0` ngay trong
--     câu (thừa nhận check chỉ phủ sổ tài chính), hoặc cho sổ phụ ghi cả hai
--     sổ (kéo theo phải định nghĩa lại đối trừ trên sổ quản trị). Neo bằng
--     `tests/test_journal_subledger_flow.py::
--     test_the_control_equation_balances_with_journal_debt`, bài ấy khẳng định
--     sổ tài chính khớp từng đồng và mọi dòng lệch còn lại đều là sổ 1.
--
-- Câu dưới đây vì thế giả định **một năm tài chính đang chạy** (năm phủ
-- `CURRENT_DATE`) và điều kiện #2 + #6 đã đóng. Đẳng thức nó đo, theo từng
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
-- `opening_balances/settlement_source.py`). Cùng phạm vi ấy được
-- `journal/settlement_service.py` giữ đúng: `_DIRECTION_BY_PARTNER_KIND`
-- không có mặt `PartnerKind.EMPLOYEE`.
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
