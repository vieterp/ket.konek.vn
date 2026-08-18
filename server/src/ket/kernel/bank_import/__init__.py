"""Nhập sao kê ngân hàng — **ngoài** khung strict-template của Excel (RT-26).

Khung ở `kernel/excel` cố ý strict: tệp đổi tên sheet hoặc tên cột thì không
nhận (FR-SYS-082), vì tệp ấy do chính hệ thống phát ra dưới dạng tệp mẫu. Sao kê
ngân hàng đi ngược lại hoàn toàn — nó do **ngân hàng** phát ra, mỗi nhà một định
dạng cột, một kiểu ngày, một kiểu dấu phân cách số. Ép sao kê vào strict-template
là một mâu thuẫn thiết kế, và RT-26 (rủi ro High của plan.md) nói thẳng nó được
miễn.

Giải: một **hồ sơ định dạng cho mỗi ngân hàng** (`bank_statement_profiles`) do
vai trò quản trị khai một lần, và bộ đọc đọc theo hồ sơ đó. Không dòng mã nào
dùng chung với `kernel/excel`, và đó là điều đúng — hai bên trả lời hai câu hỏi
trái ngược nhau về cùng một tệp .xlsx.

**Phạm vi ở phase 3 là khung.** Module Bank của phase 6 là nơi sao kê đã đọc
được biến thành chứng từ thu/chi và đối chiếu (FR-BNK-023, FR-BNK-030/031/032).
Ở đây chỉ có: bảng hồ sơ, bộ đọc, và bộ test chứng minh **hai** định dạng khác
hẳn nhau cùng chạy được qua một bộ đọc.
"""
