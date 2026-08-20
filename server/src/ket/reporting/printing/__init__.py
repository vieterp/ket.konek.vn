"""In chứng từ (FR-RPT-008/011) — phần THI HÀNH: render mẫu + kiểm soát in.

Metadata (`print_templates` + seed builtin) ở `kernel/config/printing` (C1);
gói này mang `print_log`, dịch vụ render và kiểm soát in. Ghi `print_log` là
ngoại lệ CÓ CHỦ ĐÍCH của luật "reporting chỉ đọc" (luật phụ thuộc #4): luật đó
cấm reporting ghi dữ liệu SỔ SÁCH — còn sổ theo dõi lần in là chính chức năng
FR-RPT-011 của phân hệ báo cáo, không phải một đường ghi sổ vòng.
"""

from __future__ import annotations
