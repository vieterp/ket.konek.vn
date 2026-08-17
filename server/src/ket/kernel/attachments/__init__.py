"""Đính kèm tệp vào chứng từ và danh mục (FR-NFR-053).

Tệp nằm **ngoài** cơ sở dữ liệu, DB chỉ giữ metadata. Lý do là vận hành chứ
không phải sở thích: một doanh nghiệp scan hợp đồng và hóa đơn giấy trong mười
năm sẽ có vài chục GB tệp, và nhét chúng vào `bytea` biến mỗi lần `pg_dump` của
một dataset — thứ chạy hằng đêm theo RT-03 — thành vài chục GB đọc lại từ đầu.

Ba tệp:

* `models.py` — bảng `attachments` trong schema dataset.
* `storage.py` — kho tệp định địa chỉ theo nội dung, không biết gì về DB.
* `service.py` — nối hai thứ trên, và giữ đúng thứ tự ghi đĩa/ghi bảng.
"""
