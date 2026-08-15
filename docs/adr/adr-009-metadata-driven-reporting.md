---
adr: 009
title: "Báo cáo metadata-driven; render WeasyPrint + openpyxl; go/no-go renderer phase 5"
status: accepted
date: 2026-08-15
supersedes: []
related: [FR-RPT-001, FR-NFR-044, RT-01, RT-25, "SRS 19 §9 rủi ro #2"]
---

# ADR-009: Metadata-driven reporting + WeasyPrint sandbox

## Context

**SRS 19 §9 rủi ko #2:** "Xây từng báo cáo riêng lẻ (~155 báo cáo) → chi phí phát triển bùng nổ". Không thể hardcode 155 báo cáo. **FR-RPT-001** yêu cầu "hạ tầng báo cáo dùng chung". Render báo cáo ở client (PDF/Excel) = bất khả thi (logic tính sai, performance tệ).

## Decision

**Metadata-driven report engine**:
- Bảng `report_definitions (id, dataset, layout, params, scheme)` lưu metadata
- Query: dataset định nghĩa lấy dữ liệu nào từ DB
- Template: Jinja2 HTML + CSS hoặc Excel XML
- Render phía **server**: **WeasyPrint** (HTML/CSS → PDF) + **openpyxl/xlsxwriter** (Excel)
- Client chỉ preview / in ấn (Tauri print API)

**Bảo mật (RT-01, LD-16)**:
- Template Jinja2 dùng `SandboxedEnvironment` (cấm import module, cấm gọi hàm nguy hiểm)
- WeasyPrint `url_fetcher` **chặn `file://` URL** (gỡ AD-1 Local File Inclusion)
- Gói cấu hình (ADR-008) ký số → chống giả mạo template
- SQL query trong metadata chạy như **read-only role** (RLS) → không ghi, không xóa

**RLS trên bảng gốc** (cô lập chi nhánh):
- Mỗi request, set GUC `app.tenant_branch_id = :branch_id`
- `report_definitions` query tự động lọc chi nhánh qua RLS
- Query user không được access row khác branch (**lớp phòng thủ chính**; scope_wrapper là lớp phòng thủ thứ 2)

## Consequences

### Tích cực
- 155 báo cáo = 155 row metadata + 155 template Jinja2, không 155 hàm Python
- Template không sửa → người dùng khỏi sửa code
- WeasyPrint tái dùng HTML/CSS tokens Konek (design-system web-first, ADR-003)
- RLS đúng vị trí (DB) → không dựa vào filter tầng ứng dụng (sai thiết kế)

### Tiêu cực
- **Spike S2 (WeasyPrint bundling + hiệu năng)** BẮTBUỘC cuối phase 2 (cấu hình trực tiếp: GTK/pango/cairo)
- **Go/no-go renderer** (gate RT-25): cuối phase 5 trước khi soạn 155 mẫu. Nếu WeasyPrint <10s FAIL → phải **Plan-B**:
  - Playwright / Chromium headless (có chi phí runtime, update trình duyệt)
  - Typst (ngôn ngữ markup mới, dẫn xuất CSS)
  - Xác định **plan-B** ngay bây giờ trong phase 1, không hoãn đến phase 5

## Reversal cost

Đảo sang render client-side:
- Viết lại client (React) component mỗi báo cáo (gặp lại bùng nổ 155)
- Tính sai số tiền ở client (violation FR-NFR-001 tính server)
- Hiệu năng: download dữ liệu + render trên client → tệ trên máy yếu
- PDF/Excel: dùng library client (PDF.js / SheetJS) → dung lượng bundle tăng

Đảo sang microservice báo cáo riêng:
- Tách `konek.reporting` → dịch vụ riêng
- Vận hành phức tạp (2 service, 2 deploy, sync schema)
- Không có lợi ích gì (vẫn là Python, vẫn WeasyPrint)

## Related FR

- **FR-RPT-001** (Báo cáo): metadata-driven là cơ chế chính
- **FR-NFR-044** (UI không khóa khi báo cáo): render nền, worker pool (ADR-014)
- **FR-NFR-041** (Báo cáo <10s): WeasyPrint phải đạt ngưỡng → spike S2
- **RT-01, RT-25**: Bảo mật sandbox + go/no-go renderer
- **SRS 19 §9 rủi ro #2:** 155 báo cáo → hạ tầng metadata-driven
