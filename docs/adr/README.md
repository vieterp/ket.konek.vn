# Architectural Decision Records (ADR)

Danh sách ADR cho dự án phần mềm kế toán Konek. Mỗi ADR ghi lại quyết định kiến trúc, ngữ cảnh, hệ quả, và chi phí lật quyết định.

## Danh sách ADR (ADR-001 → ADR-019)

| # | Chủ đề | Trạng thái | Neo vào | Ngày |
|---|--------|-----------|---------|------|
| ADR-001 | 3-tier client/app-server/PostgreSQL; client không nối thẳng DB | accepted | LD-01, LD-03 | 2026-08-15 |
| ADR-002 | Server Python 3.12 / FastAPI + SQLAlchemy 2.x (lệch khuyến nghị — năng lực đội); app server KHÔNG nhúng PKCS#11 | accepted | LD-03, LD-04 | 2026-08-15 |
| ADR-003 | Client Tauri + web UI TS/React; đường mở lên chế độ trình duyệt LAN | accepted | LD-02, LD-03 | 2026-08-15 |
| ADR-004 | Modular monolith 18 module + shared kernel; luật phụ thuộc ép `import-linter` | accepted | research core-arch §7 | 2026-08-15 |
| ADR-005 | Một bảng phát sinh chung `gl_postings` (append-only) thay vì sổ riêng từng module | accepted | research core-arch §1, N1 | 2026-08-15 |
| ADR-006 | Hai hệ thống sổ bằng cột `ledger`, không hai DB/schema | accepted | LD-07, FR-NFR-031, rủi ro #4 | 2026-08-15 |
| ADR-007 | Chiều phân tích: 6 cột cố định + bảng chiều mở rộng | accepted | LD-08, rủi ro #5 | 2026-08-15 |
| ADR-008 | Chế độ kế toán là gói cấu hình có hiệu lực theo ngày | accepted | LD-06, FR-NFR-055, rủi ro #1 | 2026-08-15 |
| ADR-009 | Hạ tầng báo cáo metadata-driven; render WeasyPrint/openpyxl phía server; Jinja2 `SandboxedEnvironment` + `url_fetcher` chặn `file://`; go/no-go renderer (RT-01, RT-25) | accepted | rủi ro #2, FR-RPT-001 | 2026-08-15 |
| ADR-010 | Tồn kho khóa theo (kho, VTHH, lô, serial) từ ngày đầu | accepted | LD-09, rủi ro #3 | 2026-08-15 |
| ADR-011 | Tính lại giá xuất kho có kiểm soát (đánh dấu + hàng đợi), cấm tính lại ngầm | **proposed** | rủi ro #6, FR-STK-003, RT-11 | 2026-08-15 |
| ADR-012 | Khóa chính: UUIDv7 cho chứng từ/phát sinh, `int` + `uid UUIDv7` cho danh mục | accepted | RT-19, OQ#4 (CHỐT) | 2026-08-15 |
| ADR-013 | Đánh số chứng từ: counter + SELECT FOR UPDATE (liên tục); counter lạc quan (nội bộ); idempotency key cùng txn, fail-closed | accepted | FR-NFR-004/006, RT-10/12 | 2026-08-15 |
| ADR-014 | Tính khối lượng lớn = set-based SQL; Python điều phối; worker tiến trình riêng với lease/heartbeat/reaper | accepted | LD-14, FR-NFR-041/042 | 2026-08-15 |
| ADR-015 | Kỷ luật kiểu: mypy strict + Pydantic v2 ở ranh giới; cấm `dict[str, Any]` qua module; cấm `float` (chỉ Decimal) | accepted | LD-13 | 2026-08-15 |
| ADR-016 | Ký số USB token = dịch vụ esign riêng (client-side Rust `cryptoki`); app server KHÔNG nhúng PKCS#11; ký đồng bộ lúc phát hành; outbox lưu XML đã ký + retry truyền tải | accepted | LD-04, D3, AD-1 (resolved) | 2026-08-15 |
| ADR-017 | Schema-per-dataset trong 1 PostgreSQL DB; routing schema theo dataset; handshake/đánh số/audit/RLS/backup theo schema | accepted | LD-15, D2, FR-SYS-001, RT-17 | 2026-08-15 |
| ADR-018 | LAN PKI: CA nội bộ enroll vào máy trạm (hỗ trợ chế độ trình duyệt LAN) HOẶC scope chế độ trình duyệt cần cert hợp lệ; ghi trade-off | accepted | RT-08 (chốt, không hoãn) | 2026-08-15 |
| ADR-019 | Key-management: khóa app ở OS keystore mã hóa `totp_secret`/token eSign/creds DB; chiến lược standalone/LAN/xoay khóa; backup bắt buộc mã hóa | accepted | LD-16, RT-03/05 | 2026-08-15 |

**Ghi chú trạng thái:**
- **accepted**: chốt, áp dụng ngay.
- **proposed**: chưa chốt hoàn toàn, chờ xác nhận từ stakeholder (chỉ ADR-011).

---

## Ánh xạ 6 rủi ro SRS 19 §9 → ADR xử lý

| Rủi ro # | Nội dung | ADR xử lý | Giải pháp |
|---------|----------|-----------|----------|
| #1 | Hard-code hệ thống tài khoản, mẫu BCTC, tờ khai | ADR-008 | Gói cấu hình có hiệu lực theo ngày |
| #2 | Xây từng báo cáo riêng (~155 báo cáo) → chi phí bùng nổ | ADR-009 | Hạ tầng báo cáo metadata-driven, render server-side |
| #3 | Tồn kho không tính serial/lô từ đầu → không thêm sau được | ADR-010 | Tồn kho khóa theo (kho, VTHH, lô, serial) ngay v1 |
| #4 | Hai hệ thống sổ thêm sau → sửa mọi bảng | ADR-006 | Hai sổ bằng cột `ledger` từ đầu v1 |
| #5 | Chiều phân tích cố định → mỗi ngành mới phải sửa schema | ADR-007 | 6 cột cố định + chiều mở rộng cấu hình |
| #6 | Tính giá xuất kho lùi ngày → sai giá vốn hàng loạt | ADR-011 | Tính lại có kiểm soát (đánh dấu + hàng đợi) |

---

## Quy trình viết ADR mới

### Khi nào viết ADR?

- Quyết định kiến trúc ảnh hưởng **>1 module** hoặc **thay đổi schema/API public**.
- Quyết định **không có cách lùi lại dễ dàng** (schema, khóa chính, partitioning, ...).
- Quyết định có **trade-off rõ ràng** (hiệu năng vs bảo mật, tính đơn giản vs linh hoạt, ...).

### Mẫu 5 mục (tối đa ~110 dòng/ADR)

```yaml
---
adr: NNN
title: "<tiêu đề tiếng Việt>"
status: accepted  # hoặc proposed
date: YYYY-MM-DD
supersedes: []    # [ADR-XXX] nếu lật lại ADR cũ
related: [ADR-001, ...]
---

# ADR-NNN: <tiêu đề>

## Context
(Bối cảnh: tại sao câu hỏi này lại quan trọng? Ràng buộc nào? Rủi ro nào?)

## Decision
(Chúng tôi chọn: XYZ vì A, B, C.)

## Consequences
### Tích cực
(...)
### Tiêu cực / Đánh đổi
(...)

## Reversal cost
(Nếu lật quyết định, phải sửa gì? Bảng/module nào?)

## Related FR
(FR-XXX, LD-YY, RT-ZZ, ADR-WWW)
```

### Quy tắc supersedes + related

- **supersedes**: Nếu ADR mới lật lại ADR cũ hoàn toàn, ghi `supersedes: [ADR-XXX]`.
  - Ví dụ: ADR-020 có thể supersede ADR-012 nếu chuyển khóa chính sang strategy khác.
- **related**: Tất cả ADR gọi tới, cung cấp context. Bạn cập nhật khi viết ADR.

### Review + PR

- **PR template** bắt buộc hỏi: *"Có ADR nào liên quan không? Có cần viết ADR mới không?"*
- ADR được review cùng code review (hoặc riêng nếu là quyết định kiến trúc).
- Merge ADR **trước** khi merge code liên quan (hoặc cùng lúc).

### Statuses

- **accepted**: Chốt, áp dụng ngay. Code phải tuân theo ADR.
- **proposed**: Chưa chốt, chờ xác nhận. Code có thể implement proposal, nhưng có thể đảo.
- **deprecated**: Không còn áp dụng, tham khảo lịch sử.

---

## Lịch sử ADR

| Phase | ADRs | Trạng thái |
|-------|------|-----------|
| Phase 1 | ADR-001..019 | Accepted (chốt ngày 2026-08-15, trừ ADR-011 = Proposed) |
| Phase 2+ | ADR-020, ... | Pending (tùy phase) |

---

## Tài liệu liên quan

- [`docs/system-architecture.md`](../system-architecture.md) — Kiến trúc tổng thể, topology, bộ khung.
- [`docs/code-standards.md`](../code-standards.md) — Chuẩn code (Python, TS), kiểu dữ liệu, naming.
- [`plans/260814-2204-accounting-system-architecture/plan.md`](../../plans/260814-2204-accounting-system-architecture/plan.md) — Locked Architecture Decisions (LD-01..16), Red Team Review (RT-01..27), Open Questions.

---

## Để biết thêm

Xem [ADR Lightbulb](https://adr.github.io/) hoặc [ADR Guide](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).
