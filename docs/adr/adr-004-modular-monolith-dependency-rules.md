---
adr: 004
title: "Monolith mô-đun: 18 module + kernel; luật phụ thuộc (5 ép tự động, 2 review)"
status: accepted
date: 2026-08-15
supersedes: []
related: [research-core-arch, FR-NFR-013, LD-15]
---

# ADR-004: Modular monolith + dependency rules

## Context

Hệ thống có 18 phân hệ (SRS 00 §4) + shared kernel. Nếu module tùy tiện import nhau → code bị khớp chặt, khó test, khó bảo trì, khó cô lập. Thay vì microservice (độ phức tạp vận hành), dùng **modular monolith** trong cùng một process (FastAPI), nhưng ép ranh giới module bằng `import-linter` trong CI.

## Decision

Layout:
```
ket/
  kernel/                # shared: persistence, security, config, numbering, periods, ...
  posting/               # posting engine, balances
  reporting/             # report generator
  modules/
    cash_book/, bank/, purchase/, sales/, einvoice/, inventory/, ...
```

**Luật phụ thuộc** (5 ép tự động bằng `import-linter` trong CI, 2 review):

**Tự động (import-linter):**
1. `ket.modules.*` → `ket.kernel`, `ket.posting` được; **KHÔNG** import module khác (contract C2/C3)
2. Cần dữ liệu module khác → qua **Protocol** trong `kernel` (DI injection) hoặc **domain event** (hệ quả của C3)
3. Chỉ `ket.posting` được `INSERT` vào `gl_postings` (bảng lõi) — **code review**, không ép import-linter
4. `ket.reporting` chỉ **đọc** DB, không ghi (contract C5, chỉ chặn import — không chặn access DB)
5. `ket.kernel` **KHÔNG** import `ket.modules` hay `ket.posting` (contract C1)
6. Web UI gọi **REST API + type sinh từ OpenAPI** — **eslint `no-restricted-imports`**, không phải import-linter

**Review (code review, không ép tự động):**
7. **Cấm** `dict[str, Any]` đi qua ranh giới module — dùng Pydantic model (mypy strict phát hiện được, không cần import-linter)

## Consequences

### Tích cực
- Test từng module độc lập; mock Protocol → dễ
- Reorg module sau không phá hết code (giữ kernel API)
- Dễ tách sang microservice nếu sau này cần scale 1 module
- Mọi dependency violation bắt ngay trong CI → không merge

### Tiêu cực
- Lúc đầu tay khi học design Protocol + event
- CI chạy lâu hơn (import-linter scan import)

## Reversal cost

Bỏ luật phụ thuộc:
- `import-linter.ini` xóa contract → CI không check nữa
- Nhưng đã có data, dependency cycles sẽ bùng nổ khi refactor
- Sửa code: mọi module đã mix vào nhau → phải tách output
- **Không thể đảo dễ** — nên không bỏ qua

## Related FR

- **FR-NFR-013** (Nhật ký không sửa được): kernel quản lý audit layer tách từ business module
- research-core-arch report §7
- LD-15 (Multi-dataset schema): kernel định tuyến schema độc lập
