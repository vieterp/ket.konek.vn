-- Dataset `ar_ap_open_items`: khoản công nợ theo TỪNG chứng từ — giá trị gốc,
-- số đã thanh toán **tại `:to_date`**, số còn lại. Nền của SRS 05 §5 #4/#5/#6
-- (tổng hợp / chi tiết / chi tiết theo hóa đơn của công nợ phải trả) và, qua
-- `:direction`, của SRS 06 §5.2 #1/#2/#3 ở chiều phải thu.
--
-- Một SQL phục vụ CẢ HAI chiều, đúng khuôn `ar_ap_aging` của lát 7A: chiều là
-- tham số ghim (`fixed_params`), không phải hai tệp SQL. Hai bản chép của cùng
-- phép cộng công nợ sẽ lệch nhau ở lần sửa đầu tiên.
--
-- **Hai nguồn, một mặt phẳng.** Nợ mang sang từ trước khi hệ chạy ở
-- `opening_balance_invoices` (4C), nợ do hệ sinh ra ở `ar_ap_ledger` (7A, và từ
-- 7C-3/7C-4 gồm cả nợ ghi tay bằng bút toán và khoản ứng trước). Báo cáo công nợ
-- đọc một trong hai là báo cáo SAI, và sai theo hướng khó thấy nhất vì nó vẫn ra
-- số.
--
-- **`settled` tính TẠI `:to_date`, không đọc cột cộng dồn của bảng.**
-- `ar_ap_ledger.settled` và `opening_balance_invoices.paid_amount` là hai scalar
-- **chạy**: chúng nói "đã trả bao nhiêu tính tới lúc này", không nói "đã trả bao
-- nhiêu tính tới ngày X", và cả hai bảng đều không có cột ngày đối trừ. Đọc thẳng
-- chúng cho ra một báo cáo mà **một kỳ đã khóa vẫn tự đổi số** khi có lượt trả nợ
-- ở kỳ sau: hóa đơn 1.000.000 ghi sổ 10/04, trả 400.000 ngày 20/05, đọc với
-- `to_date = 30/04` in ra "còn nợ 600.000" trong khi TK 331 trên bảng cân đối
-- 30/04 là 1.000.000. Không có gì trên tờ giấy nói rằng nó lệch — đúng hình dạng
-- lỗi mà báo cáo công nợ phải chặn, vì nó là số người ta mang đi đối chiếu với
-- nhà cung cấp.
--
-- `settled_as_of` vì thế cộng lại từ **năm bảng đối trừ** và cắt theo
-- `vouchers.posting_date`. Cùng khối ấy có mặt ở `ar_ap_aging` — hai bản chép,
-- và `test_the_two_debt_datasets_report_the_same_remaining` là cái chuông báo
-- khi chúng lệch. (Gộp hai dataset thành một là việc đúng nhưng nó rút một
-- dataset đã giao khỏi hai định nghĩa đang chạy; để lát sau.)
--
-- **Chỉ đối trừ của chứng từ ĐÃ GHI SỔ được tính.** Dòng đối trừ ghi xuống bảng
-- lúc **cất** chứng từ (`_write_settlements`), còn phần nhích `settled` của khoản
-- đích chỉ xảy ra lúc **ghi sổ** (`apply_settlements`). Cộng mọi dòng trong bảng
-- vì thế tính cả lượt trả nợ của một chứng từ còn nháp — một khoản nợ trông như
-- đã trả trong khi chưa có đồng nào lên sổ. `sv.status = 2` là phép lọc đó
-- (`DA_KHOA_SO` không bao giờ được ghi vào cột `status`, `DA_HUY` phải bị loại).
--
-- **Khoản ứng trước đầu kỳ đi NGƯỢC chiều nợ của dòng cha** — luật của chính
-- kernel, xem `SettlementTargetKind.OPENING_ADVANCE`: "ứng trước trên dòng cha
-- PHẢI THU mang chiều phải trả, và ngược lại". Bản đầu của dataset này cắt
-- `i.invoice_date <= :to_date` và vì `advance_has_no_invoice_ref` buộc dòng ứng
-- trước có `invoice_date IS NULL`, cả họ khoản ứng trước đầu kỳ **rơi khỏi báo
-- cáo** — loại bằng một tai nạn về NULL, không bằng chủ đích, và số công nợ phải
-- trả phóng lên đúng phần khách/ta đã ứng.
--
-- **`is_closed` tính trên MỘT cơ sở tiền: nguyên tệ.** Hai nhánh UNION trước đây
-- đóng theo hai cơ sở khác nhau (nhánh sổ phụ theo VND vì cột `Computed` của bảng
-- là VND, nhánh đầu kỳ theo nguyên tệ), nên với hóa đơn ngoại tệ trả từng phần
-- `open_only` giấu mất một dòng vẫn còn treo, hoặc để lọt một dòng đã hết. Nguyên
-- tệ là cơ sở đúng của câu "còn nợ hay không": phần chênh VND do làm tròn khi
-- thanh toán đi vào 515/635 (FR-SYS-066), nó không phải phần còn nợ. Cột
-- `Computed` của `ar_ap_ledger` không dùng được ở đây dù muốn — nó không trả lời
-- được câu hỏi "tại ngày X".
--
-- `amount`/`settled`/`remaining` (VND) là giá trị sổ theo tỷ giá GHI NHẬN nợ,
-- không quy đổi lại theo tỷ giá hôm nay.
--
-- **Vì sao KHÔNG có dataset "tổng hợp" riêng.** Báo cáo tổng hợp công nợ dùng
-- chính câu này với layout gộp theo đối tác (tổng nhóm là dòng tổng hợp). Một
-- dataset thứ hai chỉ để `GROUP BY partner_id` sẽ là bản chép thứ ba của khối
-- UNION hai nguồn ở trên.
--
-- *Giới hạn đã biết — khoản treo lên NHÂN VIÊN không có tên.* `partner_kind = 2`
-- trỏ `employees`, không trỏ `partners`; join thẳng sẽ in tên của một đối tác
-- trùng id (sai mà trông đúng). Cùng giới hạn và cùng lời hẹn với `ar_ap_aging`.
--
-- Tham số: :from_date (không dùng — thuộc bộ chuẩn engine luôn truyền),
-- :to_date (mốc chốt số, áp cho CẢ phát sinh nợ lẫn lượt đối trừ), :ledger,
-- :branch_ids, :direction, :partner_id, :partner_kind, :open_only.
WITH settled_as_of AS (
    -- Đã đối trừ bao nhiêu, tính tới `:to_date`. Năm bảng vì có năm phân hệ ghi
    -- lượt đối trừ; cả năm mang đúng một bộ cột, nên khối này là một phép cộng
    -- chứ không phải năm.
    --
    -- **`amount - fx_diff`, không phải `amount`.** Cột `amount` của bảng đối trừ
    -- là VND theo tỷ giá **THANH TOÁN**; phần VND thật sự giải phóng trên sổ là
    -- `amount - fx_diff`, và đó đúng là con số `apply_settlement_rows` cộng vào
    -- khoản đích (phần chênh đi vào 515/635 — FR-SYS-066). Cộng `amount` trần
    -- làm cột VND lệch trên **mọi** khoản nợ ngoại tệ trả từng phần, và lệch
    -- theo hướng tệ nhất: `settled` vượt `amount` thì `remaining` ra số ÂM trên
    -- một khoản đã tất toán. Cơ sở ghi nhận cũng là điều docstring dưới đây
    -- hứa, và là điều bản 7A làm đúng bằng `amount - settled`.
    SELECT s.target_kind,
           s.target_id,
           SUM(s.amount_fc)          AS settled_fc,
           SUM(s.amount - s.fx_diff) AS settled
    FROM (
        SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
          FROM cash_settlements
        UNION ALL
        SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
          FROM bank_settlements
        UNION ALL
        SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
          FROM purchase_settlements
        UNION ALL
        SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
          FROM sales_settlements
        UNION ALL
        SELECT voucher_id, target_kind, target_id, amount_fc, amount, fx_diff
          FROM gl_journal_settlements
    ) AS s
    -- `EXISTS` chứ không `JOIN`: khối này bị vật chất hóa (có hàm gộp, tham
    -- chiếu hai lần), và một `JOIN vouchers` buộc planner dựng hash của **mọi**
    -- chứng từ đã ghi sổ cho một báo cáo ra vài chục dòng. `EXISTS` đi khóa
    -- chính từng dòng đối trừ.
    WHERE EXISTS (
        SELECT 1
        FROM vouchers sv
        WHERE sv.id = s.voucher_id
          AND sv.status = 2
          AND sv.posting_date <= :to_date
    )
    GROUP BY s.target_kind, s.target_id
),
items AS (
    -- Nguồn 1: sổ phụ công nợ.
    --
    -- Chiều liệt kê THEO `target_kind`, nên mỗi loại đích mới phải có mặt ở CẢ
    -- HAI chỗ: `CASE` ở đây và `WHERE` bên dưới. Nới mỗi `WHERE` thì khoản phải
    -- thu ghi tay (3) rơi vào nhánh `ELSE` và hiện ở phía phải trả — hỏng nặng
    -- hơn bỏ sót nó.
    --
    -- Khoản ứng trước đi NGƯỢC loại đối tác của chính nó: tiền khách ứng trước
    -- (5) là nghĩa vụ của ta nên nằm phía phải trả, tiền ta trả trước người bán
    -- (6) là quyền của ta nên nằm phía phải thu.
    SELECT CASE l.target_kind
               WHEN 0 THEN 'thu'   -- hóa đơn bán
               WHEN 3 THEN 'thu'   -- phải thu ghi tay (GLE)
               WHEN 6 THEN 'thu'   -- trả trước người bán
               ELSE 'chi'          -- 1 hóa đơn mua, 4 phải trả ghi tay, 5 khách ứng trước
           END                          AS direction,
           l.target_kind,
           l.partner_kind,
           l.partner_id,
           l.branch_id,
           l.ledger,
           l.account_id,
           l.document_id,
           l.document_no,
           l.document_date,
           l.due_date,
           l.currency_code,
           l.amount_fc,
           l.amount,
           COALESCE(sa.settled_fc, 0)   AS settled_fc,
           COALESCE(sa.settled, 0)      AS settled
    FROM ar_ap_ledger l
    LEFT JOIN settled_as_of sa
           ON sa.target_kind = l.target_kind
          AND sa.target_id = l.id
    WHERE l.target_kind IN (0, 1, 3, 4, 5, 6)
      AND l.ledger = :ledger
      AND l.document_date <= :to_date
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR l.branch_id = ANY(:branch_ids))

    UNION ALL

    -- Nguồn 2: chi tiết chứng từ số dư ban đầu (4C), gồm **cả** khoản ứng trước
    -- đầu kỳ (`is_advance`, lát 7C-5). `target_kind` mượn đúng hai giá trị của
    -- kernel (2 số dư đầu kỳ, 7 ứng trước đầu kỳ) thay vì một giá trị tự chế:
    -- khóa tra `settled_as_of` phải khớp giá trị mà đường ghi đối trừ dùng.
    SELECT CASE b.detail_kind
               -- Luật của kernel (`SettlementTargetKind.OPENING_ADVANCE`): ứng
               -- trước đi NGƯỢC chiều nợ của dòng cha.
               WHEN 2 THEN CASE WHEN i.is_advance THEN 'chi' ELSE 'thu' END
               ELSE        CASE WHEN i.is_advance THEN 'thu' ELSE 'chi' END
           END                          AS direction,
           CASE WHEN i.is_advance THEN 7 ELSE 2 END AS target_kind,
           -- 4C cho phép bỏ trống `partner_kind`; nguồn của nó suy từ nhóm
           -- (`_DETAIL_KIND_TO_PARTNER`). Suy đúng như thế ở đây, nếu không dòng
           -- NULL sẽ trượt điều kiện join và mất tên đối tác.
           COALESCE(b.partner_kind, CASE b.detail_kind WHEN 2 THEN 0 ELSE 1 END) AS partner_kind,
           b.partner_id,
           b.branch_id,
           b.ledger,
           b.account_id,
           NULL::uuid                   AS document_id,
           i.invoice_no                 AS document_no,
           i.invoice_date               AS document_date,
           i.due_date,
           b.currency_code,
           i.amount_fc,
           i.amount,
           COALESCE(sa.settled_fc, 0)   AS settled_fc,
           COALESCE(sa.settled, 0)      AS settled
    FROM opening_balance_invoices i
    JOIN opening_balances b ON b.id = i.opening_balance_id
    JOIN fiscal_years y ON y.id = b.fiscal_year_id
    LEFT JOIN settled_as_of sa
           ON sa.target_kind = CASE WHEN i.is_advance THEN 7 ELSE 2 END
          AND sa.target_id = i.id
    WHERE b.detail_kind IN (2, 3)
      AND b.ledger = :ledger
      -- **Niên độ của số dư đầu kỳ chọn THEO TỪNG CHI NHÁNH.** Bốn bản, bốn
      -- lần sai, ghi lại cả bốn vì mỗi bản sau chỉ hẹp hơn bản trước một chút:
      --
      -- * `:to_date BETWEEN start_date AND end_date` (bản 7A) — mốc chốt ngoài
      --   mọi niên độ làm **toàn bộ nợ mang sang biến mất, im lặng**, và
      --   `validate_params` không ràng gì buộc mốc chốt nằm trong một niên độ.
      -- * "niên độ mới nhất bắt đầu không muộn hơn mốc chốt" — vẫn mất nợ khi
      --   một niên độ sau **chưa chuyển số dư sang**: nó thắng phép chọn rồi
      --   không có dòng nào.
      -- * "niên độ mới nhất **có dòng**" — vẫn mất, và mất phần khó thấy nhất:
      --   `run_carry_forward` là job **per-branch** ("chuyển số dư của chi nhánh
      --   nào là việc của người đứng ở chi nhánh đó"), nên "chi nhánh A đã
      --   chuyển, B chưa" là trạng thái vận hành **thường**. Một niên độ chọn
      --   cho cả sổ làm nợ đầu kỳ của mọi chi nhánh chưa chuyển biến mất — và
      --   lượt đọc gộp nhiều chi nhánh là lượt đọc **mặc định** (`branch_ids`
      --   trống = mọi chi nhánh trong phạm vi RLS).
      -- * bản này — mỗi chi nhánh lấy niên độ **của nó**, mới nhất trong số
      --   niên độ bắt đầu không muộn hơn mốc chốt mà chi nhánh ấy có dòng. Khi
      --   lượt chuyển số dư của một chi nhánh đã chạy, niên độ mới của chính
      --   chi nhánh ấy thắng, nên không có đường nào cộng dồn hai niên độ.
      AND y.start_date <= :to_date
      AND NOT EXISTS (
          SELECT 1
          FROM opening_balances later
          JOIN fiscal_years later_y ON later_y.id = later.fiscal_year_id
          WHERE later.branch_id = b.branch_id
            AND later.ledger = b.ledger
            AND later.detail_kind IN (2, 3)
            AND later_y.start_date <= :to_date
            AND later_y.start_date > y.start_date
      )
      -- `invoice_date IS NULL` phải ĐI QUA, không bị cắt: `advance_has_no_invoice_ref`
      -- buộc dòng ứng trước bỏ trống ngày hóa đơn, và số dư đầu kỳ thuộc niên độ
      -- đã chọn ở khối trên nên nó vốn đã trong phạm vi thời gian.
      AND (i.invoice_date IS NULL OR i.invoice_date <= :to_date)
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR b.branch_id = ANY(:branch_ids))
)
SELECT it.direction,
       it.ledger,
       it.branch_id,
       p.code                       AS partner_code,
       p.name                       AS partner_name,
       coa.code                     AS account_code,
       coa.name                     AS account_name,
       -- Nguồn của khoản nợ, nói bằng tiếng người: "hóa đơn mua" và "phải trả
       -- ghi tay" là hai thứ kế toán đối chiếu khác nhau, gộp chúng thành một
       -- cột trống là bắt người đọc mở từng chứng từ ra xem.
       CASE it.target_kind
           WHEN 0 THEN 'Hóa đơn bán'
           WHEN 1 THEN 'Hóa đơn mua'
           WHEN 2 THEN 'Số dư đầu kỳ'
           WHEN 3 THEN 'Phải thu ghi tay'
           WHEN 4 THEN 'Phải trả ghi tay'
           WHEN 5 THEN 'Khách ứng trước'
           WHEN 6 THEN 'Trả trước người bán'
           WHEN 7 THEN 'Ứng trước đầu kỳ'
           ELSE 'Loại đích ' || it.target_kind::text
       END                          AS source_label,
       it.document_no,
       it.document_date,
       it.due_date,
       -- Người mua trên hóa đơn mua (chiều gộp "theo nhân viên" của FR-PUR-040).
       -- Chỉ khoản sinh từ hóa đơn mua có người mua; nợ ghi tay, khoản ứng trước
       -- và nợ mang sang thì không, và chúng gộp vào một nhóm có tên.
       buyer.code                   AS buyer_code,
       COALESCE(buyer.name, 'Chưa khai nhân viên mua') AS buyer_name,
       it.currency_code,
       it.amount_fc,
       it.settled_fc,
       it.amount_fc - it.settled_fc AS remaining_fc,
       it.amount,
       it.settled,
       it.amount - it.settled       AS remaining
FROM items it
-- `partner_kind = 2` (nhân viên) trỏ `employees`, KHÔNG trỏ `partners`.
LEFT JOIN partners p ON p.id = it.partner_id AND it.partner_kind IN (0, 1)
LEFT JOIN chart_of_accounts coa ON coa.id = it.account_id
LEFT JOIN purchase_invoices pi ON pi.id = it.document_id AND it.target_kind = 1
LEFT JOIN employees buyer ON buyer.id = pi.buyer_id
WHERE it.direction = :direction
  -- `partner_id` một mình không đủ để nhận dạng một đối tượng công nợ: `partner_kind`
  -- = 2 trỏ `employees`, một KHÔNG GIAN ID KHÁC, nên "đối tác 47" và "nhân viên 47"
  -- là hai người và lọc theo id trộn cả hai. Khi lượt lọc không nói rõ loại, thu về
  -- danh mục `partners` — một picker chọn được `partner_id` thì nó đang chọn từ danh
  -- mục ấy. Nợ treo lên nhân viên vẫn thấy đủ khi không lọc đối tác.
  AND (
      CAST(:partner_id AS INTEGER) IS NULL
      OR (
          it.partner_id = :partner_id
          AND it.partner_kind = COALESCE(CAST(:partner_kind AS SMALLINT), it.partner_kind)
          AND (CAST(:partner_kind AS SMALLINT) IS NOT NULL OR it.partner_kind IN (0, 1))
      )
  )
  -- `open_only` mặc định KHÔNG lọc: báo cáo "chi tiết công nợ theo hóa đơn" của
  -- SRS nêu cả ba con số (giá trị / đã trả / còn phải trả), nên một hóa đơn đã
  -- trả hết vẫn là một dòng có nghĩa trong kỳ. Ai muốn chỉ phần còn treo thì
  -- ghim tham số này. Đóng theo NGUYÊN TỆ — xem docstring đầu tệp.
  AND (CAST(:open_only AS BOOLEAN) IS NOT TRUE OR it.amount_fc > it.settled_fc)
