"""Chiều phân tích mở rộng (LD-08, FR-SYS-051).

Sáu chiều **lõi** không ở đây — chúng là cột cố định trên dòng phát sinh (phase
4) và danh mục sau chúng nằm trong `kernel/master_data`. Gói này chỉ lo phần mở
rộng: chiều khai lúc chạy, không cần migration, không cần sửa mã.
"""

from __future__ import annotations
