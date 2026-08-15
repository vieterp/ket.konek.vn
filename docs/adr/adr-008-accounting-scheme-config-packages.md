---
adr: 008
title: "Chế độ kế toán = gói cấu hình có hiệu lực theo ngày (TT200/TT133)"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-06, N7, FR-NFR-055, "SRS 19 §9 rủi ro #1"]
---

# ADR-008: Config packages (TT200/TT133 by date)

## Context

**SRS 19 §9 rủi ro #1:** "Hard-code hệ thống tài khoản, mẫu BCTC, mẫu tờ khai → mỗi lần thông tư đổi phải sửa mã nguồn". Việt Nam hay đổi chế độ kế toán (TT200 ↔ TT133), mục tiêu ngân sách, thuế suất. Nếu hard-code → mỗi lần phải release bản mới, sửa DB migration.

**Yêu cầu N7 + FR-NFR-055:** Chế độ kế toán phải là **dữ liệu**, có thể chuyển đổi không sửa code.

## Decision

**Gói cấu hình (config package)** lưu toàn bộ định nghĩa của một chế độ:
- Hệ thống tài khoản (chart of accounts)
- Mẫu BCTC (layout báo cáo tài chính)
- Mẫu tờ khai (tờ khai thuế/BẢNG ĐIỀU CHỈNH)
- Mẫu chứng từ (loại chứng từ, trường bắt buộc)
- Các tham số quy định (tỷ lệ, ngưỡng)

Schema:
```sql
CREATE TABLE config_packages (
  id INT PRIMARY KEY,
  scheme VARCHAR,             -- 'TT200' hoặc 'TT133'
  is_active BOOLEAN,          -- kích hoạt bây giờ
  effective_from DATE,        -- ngày có hiệu lực
  effective_to DATE,          -- ngày hết hiệu lực (NULL = vĩnh viễn)
  is_builtin BOOLEAN,         -- built-in hay do người dùng tạo
  ...
  UNIQUE (scheme, effective_from)  -- mỗi scheme chỉ 1 gói per ngày có hiệu lực
);
CREATE TABLE config_package_templates (
  id, package_id, template_name, content (JSONB hoặc SQL)
);
```

**Ràng buộc bổ sung**: Partial unique index để đảm bảo mỗi `scheme` chỉ có 1 gói `is_active` tại một thời điểm:
```sql
CREATE UNIQUE INDEX idx_config_packages_active_scheme 
  ON config_packages(scheme) 
  WHERE is_active = TRUE;
```

**Kích hoạt**: gói A có `effective_from = 2026-01-01`, gói B `effective_from = 2026-07-01` → hệ thống tự chọn gói đúng theo ngày hiện tại (hoặc ngày người dùng chỉ định).

## Consequences

### Tích cực
- Chuyển TT200 ↔ TT133 bằng cấu hình, không sửa mã
- Mỗi gói là khóp riêng, không ảnh hưởng dữ liệu cũ (báo cáo năm 2024 theo TT200 gốc)
- Người dùng tạo gói riêng (custom account chart) không phải sửa DB schema
- Gói **ký số** (khóa publisher) → chống giả mạo cấu hình

### Tiêu cực
- Migrate gói khi thông tư mới → quản trị phức tạp
- Query cần lọc theo `effective_date` → dễ quên, lỗi
- Gói riêng = dữ liệu + code logic (SQL/template) → bảo mật cần quản lý (RLS, sandbox)

## Reversal cost

Đảo sang hard-code:
- Xóa bảng `config_packages`, `config_package_templates`
- Viết lại hệ thống TK vào migration SQL hoặc Python constant
- Viết lại mẫu BCTC vào template mã nguồn
- Xóa logic "chọn gói theo ngày" ở app startup
- **Không thể đảo nếu gói đã kích hoạt ở production** → dữ liệu cũ quên quỳ gói nào
- Mỗi lần TT đổi → phải release version mới (không "cập nhật thành dữ liệu")

## Related FR

- **FR-NFR-055** (Cập nhật không sửa mã): gói cấu hình = cơ chế chính
- **N7** (Chế độ kế toán là cấu hình)
- **LD-06** (TT200 + TT133 cùng lúc)
- **SRS 19 §9 rủi ro #1:** Hard-code → cấu hình (gói)
