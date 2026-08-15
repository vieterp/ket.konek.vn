---
adr: 012
title: "Chiến lược khóa chính: UUIDv7 cho chứng từ, int ổn định cho danh mục"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-013, ADR-017]
---

# ADR-012: Chiến lược khóa chính: UUIDv7 cho chứng từ, int ổn định cho danh mục

## Context

Phạm vi v1 là một chiều tổng hợp lên trụ sở (OQ#4 **CHỐT ngày 2026-08-15**, Validation Log), nhưng schema phải **sẵn sàng cho nối bản cài sau** (RT-19) mà không phải re-key dữ liệu.

Chứng từ + phát sinh được tạo trong từng bản cài độc lập → cần ID toàn cục duy nhất. Danh mục (tài khoản, hàng hóa, đối tác) thường được định nghĩa một lần ở trụ sở, nhân bản xuống chi nhánh → cần khóa ổn định khi sao chép.

## Decision

1. **Chứng từ + phát sinh (`vouchers`, `gl_postings`, ...)**: dùng **`UUIDv7`** làm khóa chính.
   - UUIDv7 sắp theo thời gian → tốt cho index B-tree (sequential insert).
   - **UUIDv7 sinh ở tầng ứng dụng (Python)**, không dùng hàm `uuidv7()` của
     PostgreSQL. Hàm đó chỉ có từ **PostgreSQL 18**; ép khách cài PG 18 cho một
     sản phẩm cài tại chỗ trong doanh nghiệp là rủi ro không cần thiết.
   - Kiểu `uuid` gốc của PostgreSQL dùng được từ lâu → **PG tối thiểu vẫn là 15**
     (mốc 15 đến từ `NULLS NOT DISTINCT` mà ADR-010 cần, không phải từ UUIDv7).
   - Không phụ thuộc bản cài khi nối sau.

2. **Danh mục (`accounts`, `items`, `partners`, ...)**: khóa chính là **`int` (SERIAL/IDENTITY)`**.
   - Nhỏ gọn, nhanh foreign key.
   - **Thêm cột `uid UUIDv7` UNIQUE NOT NULL** trên mọi danh mục.
   - Cột `uid` là **mã định danh ổn định** khi sao chép danh mục giữa bản cài.
   - Khi nối bản cài, sử dụng `uid` để ghép, không re-key tham chiếu.

3. **Áp dụng một chiều ngay từ phase 3**: migration thêm `uid` vào danh mục.

## Consequences

### Tích cực

- Khóa chứng từ toàn cục, không xung đột khi nối sau.
- Danh mục giữ ID `int` gọn (FK hiệu quả); `uid` là cầu nối khi cần sao chép.
- Sắp xếp thời gian tự nhiên trên `gl_postings` (UUIDv7).
- Tương thích PostgreSQL 15+ (kiểu `uuid` gốc; UUIDv7 do ứng dụng sinh nên
  không ràng buộc phiên bản DB).

### Tiêu cực / Đánh đổi

- Danh mục có thêm cột `uid` → tối thiểu lưu trữ ~16 bytes/dòng (chấp nhận được).
- Quy trình nối bản cài phức tạp (ngoài phạm vi v1, nhưng schema tính toán từ đầu).

## Reversal cost

- **Đảo về int cho chứng từ**: phải đánh lại ID → phức tạp, không khả thi nếu đã có dữ liệu.
- **Bỏ cột `uid` danh mục**: mất ổn định khi nối sau → phải làm lại quy trình sao chép.
- Phải migrate tất cả bảng con (chi tiết chứng từ) tham chiếu khóa chính.

## Related FR

- **FR-SYS-001**: Nhiều dữ liệu kế toán cô lập → schema-per-dataset (ADR-017).
- **RT-19**: Danh mục `uid UUIDv7` ổn định khi nối bản cài sau.
- **OQ#4** (CHỐT): Một chiều tổng hợp v1; cơ chế nối dự trữ ngay từ schema.
- Liên quan **ADR-013** (đánh số chứng từ dùng ID này).

---

**Ghi chú**: OQ#4 đã chốt bằng Validation Log (2026-08-15) → chiến lược này **accepted** và áp dụng từ phase 3.
