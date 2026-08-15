---
adr: 006
title: "Hai hệ thống sổ bằng cột ledger ENUM, không phải hai DB/schema"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-07, N3, FR-NFR-031, "SRS 19 §9 rủi ro #4"]
---

# ADR-006: Dual ledger (financial + management)

## Context

**SRS 19 §9 rủi ro #4:** "Hai hệ thống sổ thêm sau → phải sửa mọi bảng phát sinh và số dư". Yêu cầu N3: Sổ tài chính và Sổ quản trị lưu độc lập. Nếu thêm sau → không thể: đã ghi dữ liệu chỉ vào một sổ, tách riêng giờ là sửa xoá.

Khác biệt: Sổ tài chính cho BCTC cơ quan thuế; Sổ quản trị cho điều hành nội bộ. Cùng chứng từ ghi vào cả hai, nhưng số dư tích lũy riêng.

## Decision

Cột `ledger ENUM('financial', 'management')` trên:
- `gl_postings` (bảng phát sinh chính) — mỗi dòng ghi một lần, có ledger
- `account_balances` (snapshot số dư theo kỳ) — key gồm `(period_id, ledger, account_id, branch_id, currency_id)`
- Bảng số dư đầu kỳ `opening_balances` — cột `ledger`

**Không tạo**:
- Hai DB riêng → có thể, nhưng mất tính nhất quán, sao lưu phức tạp
- Hai schema riêng → giống, nhưng routing khó hơn, phân quyền rối
- Hai tập chứng từ riêng → sai, cùng chứng từ ghi hai sổ

## Consequences

### Tích cực
- Một bảng `gl_postings` → source of truth duy nhất, nhất quán
- Ghi sổ = 1 transaction → mọi posting 2 sổ cùng update hoặc cùng rollback
- Query: WHERE ledger = 'financial' hoặc 'management' → dễ
- Snapshot riêng mỗi sổ → tối ưu số dư đọc nhanh

### Tiêu cực
- Nếu sau này muốn "chỉ tài chính không quản trị" (v1.1) → phải lọc WHERE ledger khi xây dựng query
- Số dư snapshot 2 bảng (2x lưu trữ, 2x snapshot build) → chấp nhận được vì áp dụng FR-NFR-040 (chứng từ < 2s)

## Reversal cost

Đảo từ cột `ledger` sang hai schema/DB riêng:
- Sửa `gl_postings`, `account_balances`, `opening_balances`: THÊM schema hoặc DB column
- Sửa `ket.posting.services` logic ghi sổ: INSERT 2 lần (1 mỗi schema)
- Sửa mọi query báo cáo: UNION từ 2 schema hoặc app-level merge
- Sửa snapshot building: 2 job, 2 lịch
- Migrate dữ liệu hiện tại: tách posting 2 schema hoặc thêm DB
- **Không thể đảo nếu đã có dữ liệu ghi sổ** → phải restore from backup

## Related FR

- **FR-NFR-031** (Hai sổ): nêu rõ "độc lập cả số dư đầu kỳ lẫn phát sinh"
- **N3** (Hai hệ thống sổ song song)
- **LD-07** (Hai sổ bắt buộc ở v1)
- **SRS 19 §9 rủi ro #4:** Không sửa được sau — đã quyết định từ đầu bằng schema
