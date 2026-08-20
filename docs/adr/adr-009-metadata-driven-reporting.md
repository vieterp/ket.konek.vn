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

## Kết luận spike S2 — cổng go/no-go renderer (RT-25, đo 2026-08-20, lát 5C)

Đo trên macOS ARM (máy phát triển), WeasyPrint 69, dataset `gl_ledger` thật
(6 cột, nhóm theo TK, font Be Vietnam Pro nhúng):

| Kịch bản | Thời gian | RAM đỉnh | Ghi chú |
| --- | --- | --- | --- |
| PDF 1.000 dòng | 3,4s | 310MB | ~3,4ms/dòng — chi phí TUYẾN TÍNH theo dòng |
| PDF 3.000 dòng (cỡ một kỳ) | 10,5s | — | chạm ngưỡng FR-NFR-041 (10s) trên máy dev |
| PDF 50.000 dòng **nguyên khối** | 309s | **7,2GB** | TRƯỢT cả hai ngưỡng (60s/1GB) |
| PDF 50.000 dòng **theo lát** (1.500 dòng/lát + ghép pypdf) | 186s | **567MB** | 1.201 trang, số trang liên tục, đạt ngưỡng RAM |
| XLSX 50.000 dòng (xlsxwriter `constant_memory`) | **1,1s** | 164MB | đường xuất trọn-năm nhanh |

**Quyết định: GO cho WeasyPrint**, với hai điều kiện đã thi công ở lát 5C:

1. **Phương án phân trang là đường bắt buộc cho sổ dài** (`pdf_renderer.py`,
   ngưỡng `CHUNK_DATA_ROWS = 1500`): render từng lát, tiếp số trang bằng
   `@page:first { counter-reset: page N }`, ghép bằng pypdf, nhóm dở mở lại
   với "(tiếp theo)". RAM đỉnh ~500MB **bất kể độ dài sổ**. Chân trang chế độ
   lát là "Trang X" (không "/ Y" — tổng số trang chỉ biết sau lát cuối).
2. **Rebaseline ngưỡng thời gian sổ-cả-năm PDF** (điều RT-25 yêu cầu ghi tường
   minh): 60s không đạt được với WeasyPrint (~3,4ms/dòng là chi phí nền của
   pango/layout, `table-layout: fixed` chỉ bớt ~13%). Ngưỡng mới: **sổ cả năm
   50k dòng qua job nền ≤ 4 phút, RAM < 1GB**; đường xuất nhanh trọn năm là
   **XLSX (~1s)**. Nửa "đóng gói được" của cổng vẫn chờ spike S4 (2C-6, phần
   cứng) — pango/cairo trên máy đích là đầu vào phase 11.

Plan-B (Chromium headless / Typst) **giữ nguyên tên trong hồ sơ, chưa kích
hoạt**: chỉ mở lại nếu khách hàng thật cần PDF sổ dài nhanh hơn 4 phút — đổi
engine cho riêng nhóm sổ dài, giữ WeasyPrint cho chứng từ + BCTC (~155 mẫu
vẫn một ngôn ngữ HTML/CSS).

FR-NFR-041 (báo cáo một kỳ <10s): PDF một kỳ cỡ 3.000 dòng = 10,5s trên máy
dev — **sát ngưỡng**; lát 5E (bước 19) đo lại trên 100k chứng từ/máy đích và
quyết định hạ ngưỡng chuyển-sang-job (hiện phác thảo 20.000 dòng là quá cao
cho PDF; XLSX/JSON không bị ảnh hưởng).

## Reversal cost

Đảo sang render client-side:
- Viết lại client (React) component mỗi báo cáo (gặp lại bùng nổ 155)
- Tính sai số tiền ở client (violation FR-NFR-001 tính server)
- Hiệu năng: download dữ liệu + render trên client → tệ trên máy yếu
- PDF/Excel: dùng library client (PDF.js / SheetJS) → dung lượng bundle tăng

Đảo sang microservice báo cáo riêng:
- Tách `ket.reporting` → dịch vụ riêng
- Vận hành phức tạp (2 service, 2 deploy, sync schema)
- Không có lợi ích gì (vẫn là Python, vẫn WeasyPrint)

## Related FR

- **FR-RPT-001** (Báo cáo): metadata-driven là cơ chế chính
- **FR-NFR-044** (UI không khóa khi báo cáo): render nền, worker pool (ADR-014)
- **FR-NFR-041** (Báo cáo <10s): WeasyPrint phải đạt ngưỡng → spike S2
- **RT-01, RT-25**: Bảo mật sandbox + go/no-go renderer
- **SRS 19 §9 rủi ro #2:** 155 báo cáo → hạ tầng metadata-driven
