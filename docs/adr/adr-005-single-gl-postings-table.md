---
adr: 005
title: "Một bảng phát sinh chung gl_postings (append-only)"
status: accepted
date: 2026-08-15
supersedes: []
related: [N1, FR-NFR-001, FR-NFR-003, FR-NFR-007]
---

# ADR-005: gl_postings append-only

## Context

Sổ kế toán là trái tim yêu cầu "đúng số liệu" (SRS 19 §1). Đúng yêu cầu N1: **mọi số liệu báo cáo phải sinh từ chứng từ đã ghi sổ**. Nếu mỗi module viết vào sổ riêng (module-specific table) → không có "sổ cái" duy nhất, khó đối chiếu, khó kiểm toàn vẹn.

## Decision

**Một bảng `gl_postings` (append-only) lưu tất cả phát sinh**: bất kể từ module nào (cash_book, inventory, sales, ...). Module tạo phát sinh **qua Protocol** gọi `PostingService.post()` ở `ket.posting`, service này duy nhất được `INSERT` vào `gl_postings`.

Schema `gl_postings` cơ bản:
```sql
CREATE TABLE gl_postings (
  id UUIDv7,
  posting_date DATE,
  voucher_id UUID,         -- tham chiếu vouchers
  voucher_type VARCHAR,    -- cash_receipt, inventory_transfer, ...
  module VARCHAR,          -- cash_book, inventory, sales, ...
  branch_id INT,
  account_id INT,
  ledger ENUM('financial', 'management'),  -- ADR-006
  amount_debit NUMERIC(18,2),
  amount_credit NUMERIC(18,2),
  -- 6 chiều phân tích cố định (ADR-007)
  ...
  created_at TIMESTAMPTZ,
  created_by VARCHAR
);
-- chỉ INSERT; KHÔNG UPDATE/DELETE trên row đã tồn tại
```

Khi bỏ ghi sổ chứng từ → không xóa row, ghi row bù âm hoặc mark `reversal_id`.

## Consequences

### Tích cực
- Sổ cái là nguồn sự thật duy nhất → đối chiếu, kiểm chứng dễ
- Nhật ký (audit) tuần theo posting → trace vết ai ghi sổ
- Kiểm tra toàn vẹn (FR-NFR-007) chạy trên 1 bảng, không n bảng
- Số dư tính từ tập hợp posting → không drift nếu schema chưa đủ

### Tiêu cực
- Bảng dài theo thời gian (lưu hàng chục năm) → index cần tính toán, partition theo năm có thể cần
- KHÔNG được sửa row → nếu ghi sổ sai phải bỏ ghi + ghi lại (bỏ sung phiếu bù)

## Reversal cost

Đảo sang nhiều bảng sổ (một bảng/module):
- Sửa `PostingService.post()` → mỗi module insert riêng vào sổ riêng
- Kiểm tra toàn vẹn: không còn 1 source of truth → phải duyệt n bảng
- Viết lại query tính số dư (union n table, lag-behind nếu schema khác)
- Migrate dữ liệu: nếu đã có phát sinh ở `gl_postings` → tách vào bảng riêng
- **Không thể đảo được** nếu đã có dữ liệu

## Related FR

- **N1** (Mọi số liệu từ chứng từ ghi sổ): gl_postings append-only là cơ chế đạt N1
- **FR-NFR-001** (Decimal chính xác): NUMERIC column không dấu phẩy động
- **FR-NFR-003** (ACID giao dịch): INSERT 1 row trong 1 txn, không split
- **FR-NFR-007** (Kiểm tra toàn vẹn): balance checker chạy trên gl_postings + snapshots
