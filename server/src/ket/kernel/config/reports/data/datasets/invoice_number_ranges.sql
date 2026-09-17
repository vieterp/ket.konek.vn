-- Dataset `invoice_number_ranges`: dải số hóa đơn đã thông báo phát hành, đối
-- chiếu với số thực đã tiêu (FR-INV-002, và phần số học của BR-INV-05) — lát 7G-3.
--
-- **"Đã dùng" đếm theo SỐ HÓA ĐƠN, không theo trạng thái** — và bản đầu của lát
-- 7G-3 đếm theo trạng thái, vòng review bắt được hai lỗi từ cùng một gốc ấy.
--
-- Thứ đáng đếm là một **số đã bị tiêu khỏi dải**, và dấu hiệu duy nhất của nó là
-- `invoice_no IS NOT NULL`. Số tiêu ngay lúc cấp (RT-10) và **không nhả ra nữa**:
-- `PHAT_HANH_LOI` giữ nguyên số đã cấp, `DA_HUY` cũng giữ (BR-INV-04 cấm tái sử
-- dụng số của hóa đơn đã hủy) — nên một phép lọc theo trạng thái "từ
-- `DA_PHAT_HANH` trở lên" báo "còn trống" những số đã tiêu thật, và người dùng đi
-- xin cấp thêm dải trong khi dải cũ chưa hết.
--
-- Đếm theo trạng thái còn sai ở chiều ngược: với ký hiệu khai `provider_code`
-- (nhà cung cấp cấp số — 7E-2), tờ `DANG_PHAT_HANH` **chưa có số**, nên nó chưa
-- tiêu gì của dải mà một phép lọc `status >= 1` vẫn đếm nó.
--
-- **Số quy về ĐÚNG hồ sơ đã khai nó.** Một ký hiệu được thông báo phát hành
-- nhiều lần (xem `registration_service.effective_for`; không có `UNIQUE` trên
-- cặp (ký hiệu, chi nhánh)), nên đếm mọi hóa đơn của cặp ấy cho MỌI hồ sơ sẽ báo
-- cùng một con số "đã dùng" trên từng dòng — hai hồ sơ `1..100` và `101..200`
-- cùng in `đã dùng 3 / còn 97` trong khi hồ sơ thứ hai chưa tiêu số nào. Hồ sơ
-- khai dải thì nhận đúng những số **nằm trong dải của nó**; hồ sơ không khai dải
-- nhận phần còn lại — những số không dải nào khác của cùng cặp ấy nhận.
--
-- `invoice_no` là chuỗi (số do nhà cung cấp cấp không bảo đảm là chữ số), nên
-- phép so với dải phải qua một bộ lọc dạng trước khi ép kiểu. Số không đọc được
-- thành số nguyên thì không dải nào nhận, và nó rơi về hồ sơ không khai dải —
-- đúng chỗ của nó, vì dải số là khái niệm của hóa đơn đánh số cục bộ.
--
-- **"Đã hủy" là một LÁT CẮT của "đã dùng", không phải một cột cộng thêm.** Cộng
-- `đã dùng + đã hủy` là đếm hai lần chính những tờ ấy. Bố cục cột vì thế là:
-- đã dùng (gồm cả hủy) · trong đó đã hủy · còn lại.
--
-- **`còn lại` chỉ có nghĩa khi hồ sơ nói được TRẦN**, và hồ sơ nói được trần bằng
-- HAI cách: khai thẳng `quantity` (đường của hóa đơn giấy — FR-INV-001), hoặc khai
-- dải `range_from`/`range_to` tính cả hai đầu (FR-INV-002). Hai cách ấy là **cùng
-- một sự thật viết hai kiểu**, nên đọc mỗi `quantity` sẽ báo "không khai dải" cho
-- đúng những hồ sơ đã khai dải rành mạch — và người dùng thấy ô trống ở chỗ họ vừa
-- gõ một dải số vào.
--
-- Hồ sơ không nói được trần bằng cách nào cả thì cột này trả `NULL` chứ không trả
-- 0: "không giới hạn dải" là trạng thái hợp lệ và thường gặp của hồ sơ HĐĐT theo
-- NĐ123 (số cấp liên tục, không có trần đăng ký trước), còn 0 nghĩa là "hết số".
-- Hai câu ấy khác hẳn nhau, và in 0 cho câu đầu là dựng một cảnh báo giả.
--
-- **Đếm theo (chi nhánh, mẫu hóa đơn)**, đúng hình dạng hồ sơ: BR-INV-02 cấm dải
-- số của các chi nhánh chồng lấn, nên một phép đếm gộp cả công ty sẽ gán số của
-- chi nhánh này vào dải của chi nhánh kia và cả hai bên đều thấy sai.
--
-- Tham số: :from_date (không dùng — thuộc bộ chuẩn engine luôn truyền), :to_date
-- (mốc chốt: chỉ đếm hóa đơn có ngày không muộn hơn mốc), :branch_ids,
-- :invoice_form_id, :active_only.
SELECT r.branch_id,
       branch.code                          AS branch_code,
       branch.name                          AS branch_name,
       form.form_no,
       form.code                            AS form_code,
       COALESCE(form.name, 'Chưa khai mẫu hóa đơn') AS form_name,
       r.notice_no,
       r.notice_date,
       r.start_date,
       r.status,
       CASE r.status
           WHEN 0 THEN 'Nháp'
           WHEN 1 THEN 'Đã nộp'
           WHEN 2 THEN 'Đang hiệu lực'
           WHEN 3 THEN 'Ngừng'
           ELSE 'Trạng thái ' || r.status::text
       END                                  AS status_label,
       r.range_from,
       r.range_to,
       COALESCE(r.quantity, declared.total) AS quantity,
       used.consumed                        AS used_count,
       used.cancelled                       AS cancelled_count,
       CASE
           WHEN COALESCE(r.quantity, declared.total) IS NULL THEN NULL
           ELSE COALESCE(r.quantity, declared.total) - used.consumed
       END                                  AS remaining_count
FROM invoice_registrations r
JOIN invoice_forms form ON form.id = r.invoice_form_id
JOIN branches branch ON branch.id = r.branch_id
-- Trần suy từ dải, khi hồ sơ khai dải mà không khai số lượng. Tính CẢ HAI ĐẦU —
-- dải 1..100 là 100 số, không phải 99.
CROSS JOIN LATERAL (
    SELECT CASE
               WHEN r.range_from IS NULL OR r.range_to IS NULL THEN NULL
               ELSE r.range_to - r.range_from + 1
           END AS total
) AS declared
-- Số đã tiêu, quy về ĐÚNG hồ sơ đã khai nó. `COUNT` có lọc chứ không hai lượt
-- quét: hai câu hỏi đi cùng một lần đọc.
CROSS JOIN LATERAL (
    SELECT COUNT(*)                                AS consumed,
           COUNT(*) FILTER (WHERE e.status = 7)    AS cancelled
    FROM einvoices e
    WHERE e.invoice_form_id = r.invoice_form_id
      AND e.branch_id = r.branch_id
      -- Có số = đã tiêu một số của dải. Đây là phép lọc duy nhất về "đã dùng";
      -- trạng thái không tham gia, xem docstring đầu tệp.
      AND e.invoice_no IS NOT NULL
      -- Mốc chốt áp cho NGÀY HÓA ĐƠN. Tờ đã cấp số thì luôn có ngày, nên ở đây
      -- không cần `COALESCE` như `einvoice_register`.
      AND e.invoice_date <= :to_date
      AND (
          CASE
              WHEN r.range_from IS NOT NULL AND r.range_to IS NOT NULL THEN
                  e.invoice_no ~ '^[0-9]+$'
                  AND e.invoice_no::bigint BETWEEN r.range_from AND r.range_to
              ELSE
                  -- Hồ sơ không khai dải nhận phần còn lại: những số mà không
                  -- dải nào khác của cùng cặp (ký hiệu, chi nhánh) nhận.
                  NOT EXISTS (
                      SELECT 1
                      FROM invoice_registrations other
                      WHERE other.invoice_form_id = r.invoice_form_id
                        AND other.branch_id = r.branch_id
                        AND other.range_from IS NOT NULL
                        AND other.range_to IS NOT NULL
                        AND e.invoice_no ~ '^[0-9]+$'
                        AND e.invoice_no::bigint
                            BETWEEN other.range_from AND other.range_to
                  )
          END
      )
) AS used
WHERE (CAST(:branch_ids AS INTEGER[]) IS NULL OR r.branch_id = ANY(:branch_ids))
  AND (CAST(:invoice_form_id AS INTEGER) IS NULL OR r.invoice_form_id = :invoice_form_id)
  -- Hồ sơ hết hiệu lực vẫn là một dòng có nghĩa (nó giải thích những số đã tiêu
  -- trong quá khứ), nên mặc định KHÔNG lọc.
  AND (CAST(:active_only AS BOOLEAN) IS NOT TRUE OR r.status = 2)
