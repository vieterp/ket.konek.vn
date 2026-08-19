-- `master_data_usage` khớp tham chiếu thực tế (FR-NFR-007, BR-SYS-02).
--
-- Bộ đếm là dữ liệu DẪN XUẤT: nó chỉ trung thực khi mọi đường ghi chứng từ
-- nhớ gọi `record_use` — và chỗ bắt lỗi "quên gọi" chính là check này
-- (docstring `kernel/master_data/usage.py` chỉ thẳng sang đây).
--
-- Ở phase 4 CHƯA CÓ đường ghi nào duy trì bộ đếm (người ghi đầu tiên là
-- chứng từ phase 6), nên "tham chiếu thực tế được đếm" là tập rỗng và mọi
-- bộ đếm khác 0 đều là lệch. Khi phase 6+ bắt đầu gọi `record_use`, mỗi
-- module bổ sung nguồn đếm của mình vào đây (UNION các câu đếm theo
-- `entity_type`) — check sẽ so bộ đếm với tổng các nguồn thay vì với 0.
SELECT u.entity_type AS entity_type,
       u.entity_id   AS entity_id,
       u.usage_count AS usage_count,
       0             AS counted_references
FROM master_data_usage u
WHERE u.usage_count <> 0
