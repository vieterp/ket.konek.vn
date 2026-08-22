-- Dataset `cash_journal`: nhật ký chuyên dùng thu/chi tiền. Phủ HAI mẫu sổ
-- bằng đúng một câu, khác nhau ở tham số ghim `direction`
-- (`ReportDefinition.fixed_params`):
--
--   * `thu` → **S03a1-DN** Sổ Nhật ký thu tiền
--   * `chi` → **S03a2-DN** Sổ Nhật ký chi tiền
--
-- Hai mẫu đối xứng gương: cùng cột, cùng nguồn, đổi chiều tiền và đổi nhãn
-- "Ghi Nợ TK…"↔"Ghi Có TK…". Phase 7 dùng lại chính khuôn này cho S03a3-DN
-- (nhật ký mua hàng) và S03a4-DN (nhật ký bán hàng).
--
-- Hạt dòng = MỘT dòng phát sinh chạm TK tiền (111x/112x). `corresponding_
-- account_id` do posting engine gắn sẵn nên cột "Tài khoản khác" không phải
-- tự dò đối ứng ở đây.
--
-- **Sai khác có chủ đích so với biểu mẫu giấy:** mẫu dành các cột 2–5 cho vài
-- TK đối ứng dùng nhiều do đơn vị TỰ chọn, phần còn lại dồn vào cặp "Tài
-- khoản khác (Số hiệu / Số tiền)". Phần mềm không có bước "đơn vị chọn TK nào
-- được cột riêng" nên MỌI dòng đi vào cặp "Tài khoản khác" — đúng cấu trúc
-- mẫu, chỉ là các cột tùy chọn để trống. Hệ quả: cột 1 và cột 6 luôn bằng
-- nhau theo cấu tạo. Giữ cả hai để tờ in đúng mẫu (quyết định 5D: mã báo cáo
-- = mã mẫu thì bố cục cũng phải theo mẫu); thêm cột `money_account_code` để
-- người đọc biết cột 1 đang là TK tiền nào — mẫu giấy ghi số hiệu đó một lần
-- trên tiêu đề cột, ta có nhiều TK tiền trên cùng một sổ.
--
-- Lọc chi nhánh + sổ đi qua lớp bọc ngoài (`supports_branch/ledger = true`):
-- nhật ký không có số lũy kế nên thu hẹp phạm vi chỉ bớt dòng, không làm sai
-- con số nào còn lại.
--
-- Tham số: :from_date, :to_date, :direction ('thu'|'chi')
SELECT p.branch_id,
       p.ledger,
       p.posting_date,
       v.voucher_no,
       v.document_date,
       COALESCE(p.description, v.description) AS description,
       coa.code                               AS money_account_code,
       CASE WHEN :direction = 'thu' THEN p.debit ELSE p.credit END AS money_amount,
       corr.code                              AS other_account_code,
       CASE WHEN :direction = 'thu' THEN p.debit ELSE p.credit END AS other_amount,
       p.voucher_id,
       p.line_no,
       p.id                                   AS posting_id
FROM gl_postings p
JOIN vouchers v ON v.id = p.voucher_id
JOIN chart_of_accounts coa ON coa.id = p.account_id
LEFT JOIN chart_of_accounts corr ON corr.id = p.corresponding_account_id
WHERE p.posting_date >= :from_date
  AND p.posting_date <= :to_date
  AND (coa.code LIKE '111%' OR coa.code LIKE '112%')
  -- Chiều tiền quyết định dòng nào thuộc sổ nào; dòng phát sinh 0 đồng ở
  -- chiều đang xét không phải giao dịch của sổ này.
  AND (
        (:direction = 'thu' AND p.debit > 0)
     OR (:direction = 'chi' AND p.credit > 0)
  )
