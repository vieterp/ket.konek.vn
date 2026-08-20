"""Metadata báo cáo: một báo cáo = dataset + layout + bộ tham số (FR-RPT-001).

Đây là câu trả lời cho hai rủi ro đắt nhất của SRS 19 §9: #1 (hard-code mẫu
báo cáo) và #2 (xây ~155 báo cáo từng cái một). Báo cáo là **dữ liệu** trong
bốn bảng `report_*`; engine (`ket.reporting.engine`) chỉ đọc metadata và chạy.
Thêm một báo cáo mới = chèn dữ liệu, không sửa engine.

Vì sao nằm ở `kernel` chứ không `reporting` như phác thảo phase-05: cùng lý do
với `kernel/config/statements` (lát 5B) — dữ liệu builtin phải được gieo trong
`provision_dataset` (kernel), mà C1 cấm kernel import reporting. Phần **chạy**
báo cáo (executor, renderer) mới ở `reporting`.
"""
