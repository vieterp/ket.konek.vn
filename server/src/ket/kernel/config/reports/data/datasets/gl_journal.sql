-- Dataset `gl_journal`: Sổ Nhật ký chung (S03a-DN / S03a-DNN) — mọi dòng phát
-- sinh theo trình tự thời gian, đánh "STT dòng" liên tục như cột G của mẫu.
--
-- Khác `gl_ledger`, dataset này lọc chi nhánh **bên trong** (`supports_branch
-- = false`): `stt` là ROW_NUMBER trên tập kết quả, nên lọc ở lớp bọc bên ngoài
-- sẽ để lại dãy STT thủng lỗ. RLS vẫn là cô lập thật (RT-04) — :branch_ids chỉ
-- thu hẹp thêm trong phạm vi người gọi.
--
-- Cột "Đã ghi Sổ Cái" của mẫu giấy không có mặt: `gl_postings` chỉ chứa chứng
-- từ ĐÃ ghi sổ (BR-RPT-01) nên cột đó luôn là "x" — Điều 12 k2 TT99 cho phép
-- lược khi có Quy chế hạch toán kèm phần mềm.
--
-- Tham số: :from_date, :to_date, :ledger, :branch_ids
SELECT
    p.ledger,
    ROW_NUMBER() OVER w                    AS stt,
    p.posting_date,
    v.voucher_no,
    v.document_date,
    COALESCE(p.description, v.description) AS description,
    coa.code                               AS account_code,
    p.debit,
    p.credit
FROM gl_postings p
JOIN vouchers v ON v.id = p.voucher_id
JOIN chart_of_accounts coa ON coa.id = p.account_id
WHERE p.ledger = :ledger
  AND p.posting_date >= :from_date
  AND p.posting_date <= :to_date
  AND (CAST(:branch_ids AS INTEGER[]) IS NULL OR p.branch_id = ANY(:branch_ids))
-- Khóa sắp PHẢI chứa định danh chứng từ (review 5D, H1): đánh số per-branch
-- bảo đảm hai chi nhánh cùng cấp `GLE26-00001` — thiếu `branch_id`/`voucher_id`
-- thì dòng Nợ/Có của hai chứng từ trùng số cùng ngày ĐAN VÀO NHAU trên sổ.
WINDOW w AS (
    ORDER BY p.posting_date, v.voucher_no, v.branch_id, p.voucher_id, p.line_no, p.id
)
