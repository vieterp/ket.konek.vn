-- BR-GLE-05 ở phạm vi sổ phụ công nợ: tổng CÒN-NỢ của hai sổ phụ (số dư đầu
-- kỳ `opening_balance_invoices` + `ar_ap_ledger`) phải bằng số dư sổ cái của
-- các TK công nợ, đo theo từng (TK, đối tượng) trên **sổ tài chính**.
--
-- Tệp này viết ở 7A và **chỉ được đăng ký ở 7C-5**, sau năm lát: luật đã theo
-- suốt là viết câu trước, đăng ký sau, và chỉ đăng ký khi nó xanh trên dữ liệu
-- ĐÚNG — một check kêu sai là một check người ta học cách bỏ qua. Mười điều
-- kiện phải đóng (chín ghi sẵn từ các lát trước, một tìm thấy lúc review
-- pre-landing của chính 7C-5), và đây là chúng cùng cách đóng:
--
--  1. **Bút toán gõ thẳng vào TK công nợ** trước đây ghi `gl_postings` mang
--     chiều đối tác mà KHÔNG ghi sổ phụ nào. Đóng ở 7C-3: `general_ledger.
--     journal` thành nguồn ghi `ar_ap_ledger` thứ ba — dòng ở bên THUẬN sinh
--     khoản mới, dòng ở bên NGƯỢC đi qua `gl_journal_settlements` như một lượt
--     đối trừ. Bù trừ 131 ↔ 331 của cùng đối tác vì thế là hai lượt đối trừ,
--     không phải hai khoản nợ mới.
--
--  2. **Khoản ứng trước sinh từ CHỨNG TỪ.** Đóng ở 7C-4. `settlements` của
--     phiếu thu/chi là TÙY CHỌN, nên một phiếu thu Nợ 111 / Có 131 mang đối
--     tác mà không chọn đối trừ là khoản ứng trước hợp lệ, làm nhích vế sổ cái
--     mà không nhích vế sổ phụ. Luật chung ở `posting/debt_lines.py` đóng ca
--     ấy cho cả ba phân hệ sinh chuyển động công nợ: bên ngược không trỏ đích
--     sinh một dòng sổ phụ **chiều ngược** (`ADVANCE_FROM_CUSTOMER`/
--     `ADVANCE_TO_VENDOR`). Bù khoản ứng trước với hóa đơn phát sinh sau đi
--     bằng một chứng từ nghiệp vụ khác: dòng bên thuận trỏ vào khoản ứng
--     trước, dòng bên ngược trỏ vào hóa đơn.
--
--  3. **Chi tiết công nợ rơi lúc chuyển năm.** Đóng ở 7C-5. Lập luận 4C
--     ("`paid_amount` chưa sống tới phase 7") đã hết đúng từ 6B; phần còn thật
--     là `carry_forward_job._carry_invoices` bỏ chứng từ con khi năm mới không
--     có dòng cha cho đối tượng ấy — tức đúng ca dư RÒNG về 0 vì có khoản ứng
--     trước bù vào. Từ 7C-5 job **dựng dòng cha dư ròng 0** cho những đối
--     tượng ấy và chuyển cả hai chiều vào đó (quyết định user 2026-09-06), nên
--     hai vế cùng bằng 0 thay vì vế sổ phụ rỗng một mình.
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
--     đích (nguồn đối trừ của `receivables` cũng chỉ cộng vào sổ ấy). Đóng ở
--     7C-4 bằng **lọc `ledger = 0` ngay trong câu** (quyết định user
--     2026-09-06). Đường kia — cho sổ phụ ghi cả hai sổ — kéo theo phải định
--     nghĩa lại đối trừ trên sổ quản trị, nơi tiền thật chỉ trả một lần. Phạm
--     vi ấy vì thế là một phần của HỢP ĐỒNG câu này: công nợ chỉ sống trên sổ
--     tài chính.
--
--  7. **Khoản ứng trước ở SỐ DƯ ĐẦU KỲ.** Đóng ở 7C-5. `parsing.py` chỉ cấm
--     dòng bên ngược **mang số chứng từ**, còn bản thân khoản ứng trước vẫn
--     vào cột dư ngược của `opening_balances` — tức vế SỔ CÁI — mà trước 7C-5
--     không để lại dòng con nào ở vế sổ phụ; BR-OPB-03 nói thẳng rằng TK lưỡng
--     tính được phép như vậy, và một bài test đang XANH
--     (`test_dual_nature_partner_keeps_both_sides_on_one_parent`) làm bằng
--     chứng. Từ 7C-5 bên ngược có dòng con của chính nó
--     (`opening_balance_invoices.is_advance`, migration 0031 kèm backfill), đi
--     ra ngoài bằng `SettlementTargetKind.OPENING_ADVANCE`.
--
--  8. **Ngày chạy job ngoài mọi năm tài chính.** Đóng ở 7C-4. Câu này neo vào
--     năm phủ `CURRENT_DATE`, nhưng nhánh `ar_ap_ledger` cố ý KHÔNG lọc năm
--     (#4) — đầu tháng 1 trước khi mở niên độ mới thì vế sổ cái RỖNG còn vế sổ
--     phụ ĐẦY, và `FULL JOIN` biến **mọi** khoản công nợ đang treo thành một
--     dòng đỏ. Sửa bằng một `CROSS JOIN current_year` không kèm vị từ ngày ở
--     nhánh ấy: không lọc năm, nhưng tắt cùng lúc với vế kia.
--
-- 10. **Cửa sổ giữa "mở năm mới" và "chạy chuyển số dư".** Đóng ở 7C-5. Vế sổ
--     cái neo vào năm phủ `CURRENT_DATE`, vế sổ phụ thì không lọc năm (#4) —
--     lập luận của #4 ngầm giả định dòng cha năm mới ĐÃ tồn tại. Lượt chuyển
--     năm là job thủ công chạy sau khi khóa sổ năm cũ, nên cửa sổ ấy dài thật,
--     và trong đó mọi khoản công nợ đang treo thành một dòng đỏ. Cùng ca ấy
--     xảy ra với bút toán lùi ngày vào năm cũ sau khi đã chuyển năm. Câu **tắt
--     hẳn** trong cửa sổ đó (quyết định user 2026-09-06) thay vì kêu sai: một
--     check kêu sai vài tháng mỗi năm là một check bị tắt vĩnh viễn.
--
--  9. **Đối trừ mức CHỨNG TỪ với đối tác/TK mức DÒNG.** Đóng ở 7C-5. Phiếu
--     thu/chi và chứng từ tiền gửi đưa **tổng tiền chứng từ** vào BR-QUY-03 và
--     không truyền `account_id`, trong khi `posting_mapper` ghi sổ cái theo
--     đối tác của TỪNG DÒNG — nên phiếu thu 150 gồm Có 131 khách A 100 + Có
--     511 50, đối trừ 150 vào hóa đơn của A, là chứng từ hợp lệ để lại hai vế
--     lệch. `posting/debt_lines.settlement_scope_of` đo theo **dòng công nợ**
--     (quyết định user 2026-09-06): tổng đối trừ khớp tổng dòng công nợ, mọi
--     dòng công nợ cùng đối tác với chứng từ và cùng một TK. Nó vừa siết vừa
--     nới — phiếu thu gộp thu nợ với doanh thu trước đây không lập được.
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
-- phải thu dư Nợ nên vào dương, khoản phải trả dư Có nên vào âm. Với chi tiết
-- đầu kỳ chiều suy từ loại đối tác **đảo theo `is_advance`**; với
-- `ar_ap_ledger` chiều nằm sẵn trong `target_kind` — và đó chính là chỗ khoản
-- ứng trước phải đi ngược lại loại đối tác của nó: tiền khách ứng trước là một
-- khoản PHẢI TRẢ mang `partner_kind = 0`. Suy dấu theo `partner_kind` ở cả hai
-- nhánh sẽ cộng nó vào dương và làm lệch đúng hai lần số tiền của nó.
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
      -- **Cửa sổ chưa chuyển năm** (điều kiện #10): năm mới mở nhưng lượt
      -- chuyển số dư chưa chạy — job thủ công, thực tế chạy sau khi khóa sổ
      -- năm cũ, có khi vài tháng sau. Lúc ấy vế sổ cái chỉ có phát sinh của
      -- năm mới còn vế sổ phụ mang mọi khoản treo từ các năm trước (nhánh
      -- `ar_ap_ledger` cố ý không lọc năm, #4), nên MỌI khoản công nợ đang
      -- treo thành một dòng đỏ. Câu tắt hẳn thay vì kêu: cùng cách #8 giải
      -- bài toán ngược lại, hai vế phải bật/tắt cùng nhau.
      --
      -- Điều kiện là "có năm trước mà năm này chưa có số dư", không phải
      -- "chưa có số dư": doanh nghiệp năm đầu tiên không có số dư đầu kỳ nào
      -- là chuyện bình thường và vẫn phải được đo (vế sổ cái = phát sinh, vế
      -- sổ phụ = `ar_ap_ledger`, hai vế vẫn khớp từng đồng).
      AND (
          EXISTS (
              SELECT 1 FROM opening_balances ob
              WHERE ob.fiscal_year_id = y.id
                AND ob.branch_id = :branch_id
                AND ob.ledger = 0
          )
          OR NOT EXISTS (
              SELECT 1 FROM fiscal_years prior WHERE prior.start_date < y.start_date
          )
      )
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
    -- Chi tiết đầu kỳ: chiều suy từ loại đối tác rồi **đảo ở dòng ứng trước**
    -- (`is_advance`, lát 7C-5). Trước lát ấy bảng con chỉ chứa khoản nợ, còn
    -- khoản ứng trước nằm ở cột dư ngược của dòng CHA mà không có dòng con nào
    -- — điều kiện #7.
    SELECT ob.account_id, c.partner_kind, ob.partner_id,
           SUM((i.amount - i.paid_amount)
               * CASE c.partner_kind WHEN 0 THEN 1 ELSE -1 END
               * CASE WHEN i.is_advance THEN -1 ELSE 1 END) AS net
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
