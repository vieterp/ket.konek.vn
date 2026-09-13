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
-- **Ba sửa chữa của lát 7G-1**, cả ba là lỗi số tiền im lặng của bản 7A:
--
-- 1. **`settled` tính TẠI `:to_date`**, không đọc cột cộng dồn của bảng.
--    `ar_ap_ledger.settled` và `opening_balance_invoices.paid_amount` là hai
--    scalar **chạy** — chúng nói "đã trả tới lúc này", không nói "đã trả tới
--    ngày X", và không bảng nào có cột ngày đối trừ. Đọc thẳng chúng cho ra một
--    bảng tuổi nợ mà **một kỳ đã khóa vẫn tự đổi số** khi có lượt trả nợ ở kỳ
--    sau. Đây là con số người ta mang đi đối chiếu với nhà cung cấp.
-- 2. **Khoản ứng trước đầu kỳ đi NGƯỢC chiều nợ của dòng cha** — luật của chính
--    kernel, xem `SettlementTargetKind.OPENING_ADVANCE`. Bản 7A xếp chiều chỉ
--    theo `detail_kind`, nên một khoản **ta trả trước người bán** hiện thành một
--    khoản **ta còn nợ**: trên một dòng số dư đầu kỳ lưỡng tính (nợ 1.000.000 /
--    ứng trước 300.000) bảng tuổi nợ in 1.300.000 trong khi TK 331 ròng là
--    700.000. Cột `is_advance` đến ở 7C-5, sau khi dataset này đã viết.
-- 3. **Niên độ của số dư đầu kỳ chọn theo TỪNG CHI NHÁNH**, không phải niên độ
--    chứa `:to_date` và cũng không phải một niên độ chọn cho cả sổ — xem khối
--    `NOT EXISTS` ở nhánh số dư đầu kỳ về bốn bản và bốn lần sai.
--
-- Khối `settled_as_of` có một bản chép ở `ar_ap_open_items` (dataset SQL không
-- include được nhau); `test_the_two_debt_datasets_report_the_same_remaining` là
-- cái chuông báo khi hai bản lệch.
--
-- **Chỉ đối trừ của chứng từ ĐÃ GHI SỔ được tính**: dòng đối trừ ghi xuống bảng
-- lúc **cất** (`_write_settlements`), phần nhích `settled` chỉ xảy ra lúc **ghi
-- sổ** (`apply_settlements`) — cộng mọi dòng trong bảng là tính cả lượt trả nợ
-- của một chứng từ còn nháp.
--
-- **Giới hạn đã biết — khoản treo lên NHÂN VIÊN không có tên.** `partner_kind`
-- = 2 trỏ `employees`, không trỏ `partners`, nên phép lấy tên bên dưới bỏ qua
-- chúng: dòng ra `partner_code`/`partner_name` rỗng và gộp thành một nhóm
-- không tên trên layout. Không join bừa còn hơn in tên của một đối tác trùng
-- id (sai mà trông đúng). Mở rộng bằng một nhánh `employees` khi phân hệ tạm
-- ứng có mặt — phase 9, cùng lúc với công nợ nhân viên.
-- Tham số: :from_date, :to_date (mốc tính tuổi nợ VÀ mốc chốt số đã trả),
-- :ledger, :branch_ids, :direction (:from_date không dùng nhưng thuộc bộ chuẩn
-- engine luôn truyền).
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
open_items AS (
    -- Nguồn 1: sổ phụ công nợ (phase 7) — chứng từ mua/bán sinh, từ lát 7C-3
    -- cả chứng từ nghiệp vụ khác gõ thẳng vào TK công nợ, và từ 7C-4 cả khoản
    -- ứng trước của chứng từ tiền.
    --
    -- Chiều liệt kê THEO `target_kind`, nên mỗi loại đích mới phải có mặt ở
    -- CẢ HAI chỗ: `CASE` ở đây và `WHERE` bên dưới. Nới mỗi `WHERE` thì khoản
    -- phải thu ghi tay (3) rơi vào nhánh `ELSE` và hiện ở phía phải trả —
    -- hỏng nặng hơn bỏ sót nó.
    --
    -- Khoản ứng trước đi NGƯỢC loại đối tác của chính nó: tiền khách ứng
    -- trước (5) là nghĩa vụ của ta nên nằm ở phía phải trả, tiền ta trả trước
    -- người bán (6) là quyền của ta nên nằm ở phía phải thu. Nó không có hạn
    -- nên luôn rơi vào cột "chưa đến hạn" — đúng: không ai đòi một khoản đã
    -- nhận tiền.
    SELECT CASE l.target_kind
               WHEN 0 THEN 'thu'   -- hóa đơn bán
               WHEN 3 THEN 'thu'   -- phải thu ghi tay (GLE)
               WHEN 6 THEN 'thu'   -- trả trước người bán
               ELSE 'chi'          -- 1 hóa đơn mua, 4 phải trả ghi tay, 5 khách ứng trước
           END AS direction,
           l.partner_kind,
           l.partner_id,
           l.currency_code,
           l.document_no  AS invoice_no,
           l.document_date AS invoice_date,
           l.due_date,
           l.amount_fc - COALESCE(sa.settled_fc, 0) AS remaining_fc,
           l.amount - COALESCE(sa.settled, 0)       AS remaining
    FROM ar_ap_ledger l
    LEFT JOIN settled_as_of sa
           ON sa.target_kind = l.target_kind
          AND sa.target_id = l.id
    WHERE l.target_kind IN (0, 1, 3, 4, 5, 6)
      AND l.ledger = :ledger
      -- `is_closed` là cột `Computed` trên số cộng dồn HÔM NAY, nên nó không
      -- dùng được cho một câu hỏi "tại ngày X": một khoản đã tất toán tháng sau
      -- vẫn phải hiện trên bảng tuổi nợ của tháng này. Lọc theo phần còn treo
      -- **tại `:to_date`** thay vì theo cột ấy.
      AND l.amount_fc > COALESCE(sa.settled_fc, 0)
      AND l.document_date <= :to_date
      AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR l.branch_id = ANY(:branch_ids))

    UNION ALL

    -- Nguồn 2: chi tiết chứng từ số dư ban đầu (4C) — khoản nợ mang sang, và
    -- từ 7C-5 cả khoản ứng trước đầu kỳ (`is_advance`).
    SELECT CASE b.detail_kind
               -- Luật của kernel (`SettlementTargetKind.OPENING_ADVANCE`): ứng
               -- trước đi NGƯỢC chiều nợ của dòng cha.
               WHEN 2 THEN CASE WHEN i.is_advance THEN 'chi' ELSE 'thu' END
               ELSE        CASE WHEN i.is_advance THEN 'thu' ELSE 'chi' END
           END AS direction,
           -- 4C cho phép bỏ trống `partner_kind`; nguồn của nó suy từ nhóm
           -- (`_DETAIL_KIND_TO_PARTNER`). Suy đúng như thế ở đây, nếu không
           -- dòng NULL sẽ trượt điều kiện join và mất tên đối tác.
           COALESCE(b.partner_kind, CASE b.detail_kind WHEN 2 THEN 0 ELSE 1 END) AS partner_kind,
           b.partner_id,
           b.currency_code,
           i.invoice_no,
           i.invoice_date,
           i.due_date,
           i.amount_fc - COALESCE(sa.settled_fc, 0) AS remaining_fc,
           i.amount - COALESCE(sa.settled, 0)       AS remaining
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
      AND i.amount_fc > COALESCE(sa.settled_fc, 0)
      -- Cùng phép cắt ngày với `ar_ap_open_items`: 4C không ràng ngày hóa đơn
      -- của dòng chi tiết phải trước niên độ, nên một dòng ghi ngày sau mốc chốt
      -- hiện ở bảng tuổi nợ mà không hiện ở bảng chi tiết — hai tờ giấy nói hai
      -- điều về cùng một khoản. `IS NULL` phải đi qua: `advance_has_no_invoice_ref`
      -- buộc dòng ứng trước bỏ trống ngày hóa đơn.
      AND (i.invoice_date IS NULL OR i.invoice_date <= :to_date)
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
       -- Thứ tự ĐỌC của nhóm tuổi nợ, vì thứ tự chữ cái của `bucket` sai hẳn:
       -- '1-30' < '31-60' < '61-90' < 'chua-den-han' < 'khong-han' < 'tren-90'
       -- xếp nhóm chưa đến hạn vào giữa các nhóm quá hạn. Layout nào gộp theo
       -- nhóm tuổi nợ phải sắp bằng cột này.
       CASE
           WHEN oi.due_date IS NULL              THEN 0
           WHEN oi.due_date >= :to_date          THEN 1
           WHEN oi.due_date >= :to_date - 30     THEN 2
           WHEN oi.due_date >= :to_date - 60     THEN 3
           WHEN oi.due_date >= :to_date - 90     THEN 4
           ELSE 5
       END AS bucket_seq,
       oi.currency_code,
       oi.remaining_fc,
       oi.remaining
FROM open_items oi
-- `partner_kind = 2` (nhân viên) trỏ `employees`, KHÔNG trỏ `partners`:
-- join thẳng sẽ in tên của một đối tác trùng id — sai mà trông đúng.
LEFT JOIN partners p ON p.id = oi.partner_id AND oi.partner_kind IN (0, 1)
WHERE oi.direction = :direction
ORDER BY p.code, oi.due_date NULLS LAST, oi.invoice_no
