---
adr: 007
title: "Chiều phân tích: 6 cột cố định + bảng chiều mở rộng"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-08, N6, "SRS 19 §9 rủi ro #5"]
---

# ADR-007: Analytic dimensions (6 fixed + extensible)

## Context

**SRS 19 §9 rủi ro #5:** "Chiều phân tích cố định thay vì cấu hình → mỗi ngành mới lại phải sửa schema". Doanh nghiệp Việt có nhu cầu phân tích theo nhiều chiều: chi nhánh, đối tượng THCP, công trình, đơn hàng, hợp đồng, khoản mục CP, và có thể thêm sau (vd mã nhân viên, mã khách hàng, mã nhà cung cấp).

**Yêu cầu N6:** Hệ thống phải **cấu hình chiều**, không bắt buộc nhập tất cả.

## Decision

**Chiều lõi 6 cột** (cố định trên `gl_postings`):
1. `branch_id` — chi nhánh
2. `cost_object_id` — đối tượng THCP (khách, NCC, nhân viên, ...)
3. `project_id` — công trình / vụ việc
4. `order_id` — đơn hàng / hợp đồng bán
5. `contract_id` — hợp đồng mua / mô hình
6. `cost_item_id` — khoản mục chi phí (cho giá thành)

**Chiều mở rộng**: Bảng `posting_dimension_values (posting_id, dimension_id, value_id)`:
- Khi người dùng khai báo chiều mới (vd "mã nhân viên"), thêm row trong `dimensions`
- Lúc ghi sổ, nếu có giá trị → INSERT vào `posting_dimension_values`
- Query báo cáo: JOIN bảng này để filter theo chiều mở rộng
- Hiệu năng: snapshot chỉ giữ 6 chiều cố định (FR-NFR-041 <10s); chiều mở rộng query trực tiếp `gl_postings` + `posting_dimension_values`

## Consequences

### Tích cực
- 6 chiều cố định → tối ưu tốc độ; index, snapshot, query partition dễ dàng
- Chiều mở rộng → linh hoạt ngành; không phải sửa schema
- Doanh nghiệp không cần mỗi chiều lại "phải cấu hình bắt buộc" → chọn lọc khai báo

### Tiêu cực
- 6 chiều có thể chưa đủ → người dùng phàn nàn "sao không có mã nhân viên cột"
- Chiều mở rộng query chậm hơn (có bảng thêm) → dùng sau khi phase 5 (hạ tầng báo cáo) confirm tốc độ
- Maintain 2 đường: cột cố định logic + bảng mở rộng logic

## Reversal cost

Đảo từ 6 cột cố định sang **động hoàn toàn** (mọi chiều bảng mở rộng):
- Migrate dữ liệu: mỗi `gl_postings` row 6 chiều → INSERT 6 row `posting_dimension_values`
- Lấy dữ liệu: mọi query phải JOIN bảng mở rộng → tốc độ giảm ~5-10x (FR-NFR-041 không đạt)
- Index strategy: phải index dimension + value để tìm nhanh
- **Không thể đảo nếu FR-NFR-041 là tiêu chí** (< 10s)

Đảo sang **cột động** (ALTER TABLE thêm cột):
- Migrate: mỗi lần thêm chiều → ALTER TABLE (nếu dữ liệu lớn → downtime)
- Index: tự động index cột? hay manual? → rối
- Hiệu năng: cột thêm sau thường để NULL → lãng phí

## Related FR

- **FR-NFR-041** (Báo cáo <10s): 6 cột cố định đạt target; chiều mở rộng phải verify phase 5
- **N6** (Kỳ kế toán khóa được): chiều có ảnh hưởng khóa sổ theo chiều? (không trong v1)
- **LD-08** (Chiều phân tích ở v1)
- **SRS 19 §9 rủi ro #5:** Không sửa schema được → chuẩn bị từ đầu bằng bảng mở rộng
- **SRS 20 §1**: Đặc thù ngành dùng chiều mở rộng, không code riêng
