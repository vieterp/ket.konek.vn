---
adr: 010
title: "Tồn kho khóa theo (chi nhánh, kho, VTHH, lô/serial) từ ngày đầu"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-09, FR-STK-001, "SRS 19 §9 rủi ro #3"]
---

# ADR-010: Inventory balances (lot/serial from day one)

## Context

**SRS 19 §9 rủi ro #3:** "Thiết kế tồn kho không tính đến serial/số lô từ đầu → không thêm sau được". Nếu bảng `inventory_balances` chỉ khóa `(branch, warehouse, item)`, sau này muốn theo lô/serial → phải:
- Alter bảng (downtime, schema thay đổi)
- Migrate dữ liệu cũ (làm sao gán lô/serial cho dòng cũ?)
- Viết lại tất cả query tồn kho (WHERE dòng đơn khác WHERE với lô)

**Quyết định LD-09:** Đặt vấn đề tồn kho lô/serial ngay bản đầu, UI/báo cáo lô ở v1.1 (hoãn được, nhưng schema sẵn).

## Decision

**Bảng `inventory_balances`** khóa composite:
```sql
CREATE TABLE inventory_balances (
  id UUID PRIMARY KEY,        -- khóa thay thế (UUIDv7)
  branch_id INT,
  warehouse_id INT,
  item_id INT,
  lot_id UUID NULL,           -- null = lô/serial chưa được theo dõi
  serial_id UUID NULL,        -- null = không serial
  --
  balance_date DATE,
  period_id INT,
  quantity NUMERIC(18,4),
  cost NUMERIC(18,2),
  ...
  UNIQUE (branch_id, warehouse_id, item_id, lot_id, serial_id, balance_date) 
    NULLS NOT DISTINCT  -- PostgreSQL 15+: cho phép NULL trong UNIQUE, so sánh NULL = NULL
);
```

**Phương án thay thế (PostgreSQL <15)**: Nếu dùng phiên bản cũ hơn, dùng **sentinel value** (`'00000000-0000-0000-0000-000000000000'`) thay NULL để tránh bị loại khỏi UNIQUE index.

**Quy tắc**:
- `lot_id IS NULL` và `serial_id IS NULL` → theo dõi tồn kho **chung**, không phân biệt lô/serial
- Khi nhập hàng với lô/serial → tạo row riêng
- Nếu doanh nghiệp chưa theo dõi → UI ẩn cột lô/serial, tất cả null

**Tồn kho phát sinh (`gl_postings`)**: cột `inventory_lot_id`, `inventory_serial_id` lưu từ lúc INSERT. Lúc build snapshot `inventory_balances` → GROUP BY all 5 cột (branch + warehouse + item + lot + serial).

## Consequences

### Tích cực
- Schema sẵn từ đầu → thêm lô/serial ở v1.1 không phải migrate
- Query không phải sửa (SELECT GROUP BY 5 cột, không GROUP BY 3)
- Snapshot build 1 lần (tất cả trường hợp)
- Phù hợp với quy định kế toán lô/serial (hàng hóa tàu, dược phẩm)

### Tiêu cực
- Nếu không theo dõi lô/serial → tất cả row có NULL (nhẹ, chấp nhận được)
- Snapshot `inventory_balances` rộng hơn (more columns, more storage) → chấp nhận

## Reversal cost

Đảo sang **không theo dõi lô/serial** (khóa chỉ 3 cột):
- Alter `inventory_balances` DROP cột lô/serial
- Viết lại query tồn kho: GROUP BY 3 thay 5
- Xóa cột `inventory_lot_id`, `inventory_serial_id` từ `gl_postings`
- Migrate dữ liệu cũ: hợp nhất row lô/serial → cộng chung tồn kho
- **Không thể đảo nếu đã nhập dữ liệu lô/serial** → mất thông tin lô

Đảo sang **bảng lô/serial riêng** (thay vì cột khóa):
- Tạo bảng `inventory_lot_allocations (item_id, lot_id, quantity_allocated_from_date)`
- Query tồn kho: JOIN từ `inventory_balances` sang `inventory_lot_allocations`
- Hiệu năng giảm (thêm 1 JOIN)
- Không được, rủi ro tương tự

## Related FR

- **FR-STK-001** (Quản lý kho): có cột lô/serial sẵn
- **FR-STK-003** (Tính lại giá xuất kho): cần biết lô nào → FIFO/LIFO chính xác
- **LD-09** (Lô/serial từ ngày đầu)
- **SRS 19 §9 rủi ro #3:** Không sửa được sau → schema sẵn từ ngày 1
- **SRS 09 / SRS 11** (Kho / TSCĐ): theo dõi lô/serial nếu cần
